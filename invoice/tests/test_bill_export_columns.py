"""``billExport`` writes the bills ``bill`` lists to the same reader, and an export column is one of the bill's own
columns, a key of one of its JSON columns or a relation column the frontend requests; any other column is refused
before any query, naming it."""
import csv
import io
import json

from django.contrib.contenttypes.models import ContentType
from django.core.cache import caches
from django.test import override_settings

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.models import Individual
from invoice.models import Bill
from location.models import Location

PERMS = ["gql_bill_search_perms", "gql_individual_search_perms"]
FRONTEND_COLUMNS = ["id", "userCreated.username", "amount_total", "subjectType", "subjectId", "thirdpartyType",
                    "thirdpartyId", "code", "status", "dateBill"]


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01",
                                   audit_user_id=-1)


@override_settings(ROW_SECURITY=True)
class BillExportColumnsTest(openIMISGraphQLTestCase):
    """The bill BXP-P is for a person of district D, BXP-Q for a person of D2; the reader is limited to D2."""

    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        region = make_location("BXP-R", "R")
        village = make_location("BXP-V", "V", make_location("BXP-W", "W", make_location("BXP-D", "D", region)))
        village2 = make_location("BXP-V2", "V", make_location("BXP-W2", "W", make_location("BXP-D2", "D", region)))
        clear_location_cache()
        self.admin = create_right_only_user("bxp_admin", PERMS, ["BXP-D", "BXP-D2"])
        self.reader = create_right_only_user("bxp_reader", PERMS, ["BXP-D2"])
        person_type = ContentType.objects.get_for_model(Individual)
        for code, last_name, location in (("BXP-P", "Bxppind", village), ("BXP-Q", "Bxpqind", village2)):
            person = Individual(first_name="Bxp", last_name=last_name, dob="1990-01-01", location=location)
            person.save(user=self.admin)
            Bill(subject_type=person_type, subject_id=str(person.id), code=code, status=Bill.Status.VALIDATED,
                 currency_tp_code="BIF", currency_code="BIF", json_ext={"ref": code}).save(user=self.admin)
        clear_location_cache()

    def gql(self, user, query):
        token = BaseTestContext(user=user).get_jwt()
        return json.loads(self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"}).content)

    def export(self, user, fields):
        """The response of the export and the rows of the files it writes."""
        before = set(ExportableQueryModel.objects.values_list("id", flat=True))
        content = self.gql(user, '{ billExport(fields: %s, fieldsColumns: "{}") }' % json.dumps(fields))
        rows = []
        for file in ExportableQueryModel.objects.exclude(id__in=before):
            file.content.open("rb")
            rows += list(csv.DictReader(io.StringIO(file.content.read().decode())))
        return content, rows

    REFUSED = (
        ("userCreated.individualUserCreated.lastName", "user_created__individual_user_created__last_name"),
        ("subjectType.subjectTypeBill.code", "subject_type__subject_type_bill__code"),
        ("userCreated.billUserCreated.code", "user_created__bill_user_created__code"),
        ("lineItemsBill.code", "line_items_bill__code"),
    )

    def test_an_export_column_outside_the_allowlist_is_refused_naming_the_column(self):
        for field, column in self.REFUSED:
            for user in (self.reader, self.admin):
                with self.subTest(column=column, user=user.username):
                    files = ExportableQueryModel.objects.count()
                    content, rows = self.export(user, ["subject_type", "subject_id", field])
                    self.assertEqual(rows, [])
                    self.assertEqual(ExportableQueryModel.objects.count(), files)
                    self.assertIn(f"the export column {column} is not exportable", json.dumps(content["errors"]))

    def test_the_export_writes_the_bills_the_list_gives_the_same_reader(self):
        listed = self.gql(self.reader, '{ bill { edges { node { code } } } }')
        self.assertEqual([edge["node"]["code"] for edge in listed["data"]["bill"]["edges"]], ["BXP-Q"])
        _content, rows = self.export(self.reader, ["subject_type", "subject_id", "code"])
        self.assertEqual([row["code"] for row in rows], ["BXP-Q"])
        _content, rows = self.export(self.admin, ["subject_type", "subject_id", "code"])
        self.assertEqual(sorted(row["code"] for row in rows), ["BXP-P", "BXP-Q"])

    def test_the_reader_gets_no_person_or_bill_of_another_district_through_an_export_column(self):
        for field, _column in self.REFUSED[:3]:
            with self.subTest(column=field):
                _content, rows = self.export(self.reader, ["subject_type", "subject_id", field])
                self.assertNotIn("Bxppind", json.dumps(rows))
                self.assertNotIn("BXP-P", json.dumps(rows))

    def test_the_columns_the_frontend_requests_still_export(self):
        content, rows = self.export(self.reader, FRONTEND_COLUMNS)
        self.assertNotIn("errors", content)
        self.assertEqual([(row["code"], row["user_created__username"]) for row in rows],
                         [("BXP-Q", self.admin.username)])

    def test_the_bill_own_columns_and_json_keys_export(self):
        content, rows = self.export(self.reader, ["subject_type", "subject_id", "currency_code", "json_ext",
                                                  "jsonExt.ref", "userCreatedId"])
        self.assertNotIn("errors", content)
        self.assertEqual([(row["currency_code"], row["json_ext__ref"]) for row in rows], [("BIF", "BXP-Q")])
