"""The subject of a bill and the line of a bill item are the whole row of the object they point at. For a reader
without the masked-data right they are masked by the mask registered for that object's model."""
import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from core.data_masking import DataMaskAbs, MaskingClassRegistryPoint
from core.test_helpers import create_right_only_user, create_test_officer
from invoice.gql.gql_types.bill_types import BillGQLType, BillItemGQLType
from invoice.models import Bill, BillItem
from invoice.utils import underscore_to_camel

MASKED = DataMaskAbs().anonymize("")
UNMASK = "gql_query_enable_viewing_masked_data_perms"


class OfficerMask(DataMaskAbs):
    masking_model = "Officer"
    anon_fields = ["last_name", "json_ext.phone"]
    masking_enabled = True


class BillSubjectMaskingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = create_right_only_user("bill_mask_viewer", [])
        cls.unmasked = create_right_only_user("bill_mask_unmasked", [UNMASK])
        cls.officer = create_test_officer(custom_props={
            "code": "BMASK1", "last_name": "N'Dayi-Ke", "other_names": "Aimee",
            "json_ext": {"phone": "+25779000000", "zone": "Z1"}})
        officer_type = ContentType.objects.get_for_model(cls.officer)
        bill = Bill(subject_type=officer_type, subject_id=str(cls.officer.id), code="BMASK-B",
                    status=Bill.Status.VALIDATED, currency_tp_code="BIF", currency_code="BIF")
        bill.save(user=cls.unmasked)
        cls.bill_id = bill.id
        item = BillItem(bill=bill, line_type=officer_type, line_id=str(cls.officer.id), code="BMASK-L",
                        quantity=1, unit_price=10, amount_total=10, amount_net=10)
        item.save(user=cls.unmasked)
        cls.item_id = item.id

    def setUp(self):
        registry = mock.patch.object(MaskingClassRegistryPoint, "REGISTERED_MASKING_CLASS",
                                     list(MaskingClassRegistryPoint.REGISTERED_MASKING_CLASS))
        registry.start()
        self.addCleanup(registry.stop)
        MaskingClassRegistryPoint.register_masking_class([OfficerMask()])

    def read(self, user):
        info = SimpleNamespace(context=SimpleNamespace(user=user))
        bill = Bill.objects.get(id=self.bill_id)
        item = BillItem.objects.get(id=self.item_id)
        return (json.loads(BillGQLType.resolve_subject(bill, info)),
                json.loads(BillItemGQLType.resolve_line(item, info)))

    def test_the_subject_and_line_are_masked_without_the_masked_data_right(self):
        for row in self.read(self.viewer):
            self.assertEqual(row["lastName"], MASKED)
            self.assertEqual(row["jsonExt"], {"phone": MASKED, "zone": "Z1"})
            self.assertEqual(row["otherNames"], "Aimee")
            self.assertEqual(row["code"], "BMASK1")

    def test_the_subject_and_line_are_clear_with_the_masked_data_right(self):
        for row in self.read(self.unmasked):
            self.assertEqual(row["lastName"], "N'Dayi-Ke")
            self.assertEqual(row["jsonExt"], {"phone": "+25779000000", "zone": "Z1"})

    def test_the_masked_reading_leaves_the_object_clear(self):
        bill = Bill.objects.get(id=self.bill_id)
        BillGQLType.resolve_subject(bill, SimpleNamespace(context=SimpleNamespace(user=self.viewer)))
        self.assertEqual(bill.subject.last_name, "N'Dayi-Ke")
        self.assertEqual(bill.subject.json_ext["phone"], "+25779000000")


class BillMask(DataMaskAbs):
    masking_model = "Bill"
    anon_fields = ["terms", "json_ext.phone"]
    masking_enabled = True


class HistoryModelSubjectMaskingTest(TestCase):
    """A subject built on core's HistoryModel keeps private attributes (the dirty-fields original state) next to its
    columns; the row read for the subject or line holds the columns only."""

    @classmethod
    def setUpTestData(cls):
        cls.viewer = create_right_only_user("bill_hmask_viewer", [])
        cls.unmasked = create_right_only_user("bill_hmask_unmasked", [UNMASK])
        cls.source = Bill(code="HMASK-S", status=Bill.Status.VALIDATED, currency_tp_code="BIF", currency_code="BIF",
                          terms="N'Dayi-Ke", json_ext={"phone": "+25779000000", "zone": "Z1"})
        cls.source.save(user=cls.unmasked)
        bill_type = ContentType.objects.get_for_model(Bill)
        bill = Bill(subject_type=bill_type, subject_id=str(cls.source.id), code="HMASK-B",
                    status=Bill.Status.VALIDATED, currency_tp_code="BIF", currency_code="BIF")
        bill.save(user=cls.unmasked)
        cls.bill_id = bill.id
        item = BillItem(bill=bill, line_type=bill_type, line_id=str(cls.source.id), code="HMASK-L",
                        quantity=1, unit_price=10, amount_total=10, amount_net=10)
        item.save(user=cls.unmasked)
        cls.item_id = item.id

    def setUp(self):
        registry = mock.patch.object(MaskingClassRegistryPoint, "REGISTERED_MASKING_CLASS",
                                     list(MaskingClassRegistryPoint.REGISTERED_MASKING_CLASS))
        registry.start()
        self.addCleanup(registry.stop)
        MaskingClassRegistryPoint.register_masking_class([BillMask()])
        self.columns = {underscore_to_camel(field.attname) for field in Bill._meta.concrete_fields}

    def read(self, user):
        info = SimpleNamespace(context=SimpleNamespace(user=user))
        return (json.loads(BillGQLType.resolve_subject(Bill.objects.get(id=self.bill_id), info)),
                json.loads(BillItemGQLType.resolve_line(BillItem.objects.get(id=self.item_id), info)))

    def test_the_row_holds_the_masked_columns_only_without_the_masked_data_right(self):
        for row in self.read(self.viewer):
            self.assertEqual(set(row), self.columns)
            self.assertEqual(row["terms"], MASKED)
            self.assertEqual(row["jsonExt"], {"phone": MASKED, "zone": "Z1"})
            self.assertNotIn("N'Dayi-Ke", json.dumps(row))
            self.assertNotIn("+25779000000", json.dumps(row))

    def test_the_row_holds_the_columns_only_with_the_masked_data_right(self):
        for row in self.read(self.unmasked):
            self.assertEqual(set(row), self.columns)
            self.assertEqual(row["terms"], "N'Dayi-Ke")
            self.assertEqual(row["jsonExt"], {"phone": "+25779000000", "zone": "Z1"})

    def test_the_subject_and_line_can_be_read_twice_from_the_same_object(self):
        info = SimpleNamespace(context=SimpleNamespace(user=self.unmasked))
        bill = Bill.objects.get(id=self.bill_id)
        item = BillItem.objects.get(id=self.item_id)
        self.assertEqual(BillGQLType.resolve_subject(bill, info), BillGQLType.resolve_subject(bill, info))
        self.assertEqual(BillItemGQLType.resolve_line(item, info), BillItemGQLType.resolve_line(item, info))
