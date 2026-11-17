import graphene
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db.models import JSONField, Q
from pandas import DataFrame

from core.gql.export_mixin import ExportableQueryMixin
from core.schema import OrderedDjangoFilterConnectionField
from core.utils import append_validity_filter
from invoice.apps import InvoiceConfig
from invoice.gql.gql_types.bill_types import BillGQLType
from invoice.models import Bill
import graphene_django_optimizer as gql_optimizer
from django.apps import apps
try:
    Policy = apps.get_model('policy','Policy')
except:
    Policy = {}


def patch_subjects(bills_df: DataFrame):
    subject_df = bills_df[['subject_type', 'subject_id']].drop_duplicates()
    bills_df['subject_class'] = 'undefined'
    for subject in subject_df['subject_type'].unique():
        model = ContentType.objects.get(id=subject).model_class()
        entities_for_model = subject_df[subject_df['subject_type'] == subject]['subject_id']
        if model == Policy:
            subject_names = model.objects \
                .filter(id__in=entities_for_model) \
                .values('id', 'family__head_insuree__chf_id',
                        'family__head_insuree__last_name', 'family__head_insuree__other_names')
            names = {
                str(x['id']): F"{x['family__head_insuree__other_names']} {x['family__head_insuree__last_name']} ({x['family__head_insuree__chf_id']})"
                for x in subject_names
            }
            updated = bills_df['subject_type'] == subject
            bills_df.loc[updated, 'subject_id'] = bills_df[updated]['subject_id'].apply(lambda x: names[x])
        bills_df.loc[bills_df['subject_type'] == subject, 'subject_class'] = model.__name__
        bills_df['subject_type'] = bills_df.pop('subject_class')
    return bills_df


class BillQueryMixin(ExportableQueryMixin):
    export_patches = {
        'bill': [
            patch_subjects
        ]
    }

    exportable_fields = ['bill']
    # The columns of an export that follow a relation, by exported field: those the frontend requests.
    export_relation_columns = {'bill': ('user_created__username',)}

    @classmethod
    def check_export_columns(cls, field_name, model, columns):
        """Refuses an export whose ``columns`` name a column other than ``model``'s own columns, the keys of its
        JSON columns and the ``export_relation_columns`` of ``field_name``: a column following any other relation
        reads rows outside the reader's scope."""
        concrete = model._meta.concrete_fields
        allowed = {name for field in concrete for name in (field.name, field.attname)}
        allowed.update(cls.export_relation_columns.get(field_name, ()))
        json_columns = {field.name for field in concrete if isinstance(field, JSONField)}
        for column in columns:
            if column not in allowed and column.split("__", 1)[0] not in json_columns:
                raise PermissionDenied(f"the export column {column} is not exportable")

    @classmethod
    def create_export_function(cls, field_name):
        super().create_export_function(field_name)
        resolver_name = f"resolve_{field_name}_export"
        core_exporter = getattr(cls, resolver_name)

        def exporter(root, info, **kwargs):
            cls.check_export_columns(field_name, getattr(cls, field_name).model,
                                     [cls._adjust_notation(column) for column in kwargs.get("fields") or []])
            return core_exporter(root, info, **kwargs)

        setattr(cls, resolver_name, staticmethod(exporter))

    bill = OrderedDjangoFilterConnectionField(
        BillGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        subject_type_filter=graphene.String(),
        thirdparty_type_filter=graphene.String(),
    )

    def resolve_bill(self, info, **kwargs):
        BillQueryMixin._check_permissions(info.context.user)
        filters = []
        filters += append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        subject_type = kwargs.pop("subject_type_filter", None)
        if subject_type:
            filters.append(Q(subject_type__model=subject_type))

        thirdparty_type = kwargs.get("thirdparty_type_filter", None)
        if thirdparty_type:
            filters.append(Q(thirdparty_type__model=thirdparty_type))

        qs = Bill.get_queryset(Bill.objects.filter(*filters), info.context.user)
        if InvoiceConfig.bill_user_filter:
            qs = InvoiceConfig.bill_user_filter(qs, info.context.user)

        return gql_optimizer.query(qs, info)

    @staticmethod
    def _check_permissions(user):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                InvoiceConfig.gql_bill_search_perms):
            raise PermissionError("Unauthorized")
