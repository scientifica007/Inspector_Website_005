"""Idempotent demonstration data.

``python manage.py seed_demo``

This command is a convenience for trying the platform out; the application runs
without it. Re-running it is safe: it reuses existing rows and never destroys
data. Pass ``--reset`` to delete and rebuild *only* the demo records it owns.

Demo credentials (change them before any real use):
    admin      / admin-demo-123
    inspector  / inspector-demo-123
    inspector2 / inspector2-demo-123
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Role, User
from catalog.models import NodeType, ReferenceVisibility, ValueType
from catalog.services import add_node, create_reference
from governance.models import Guide, GuideStatus, GuideTarget
from institutions.models import Institution, InstitutionKind
from visits.models import ItemResult
from visits.services import create_visit, record_result, select_node

DEMO_ADMIN = "admin"
DEMO_INSPECTOR = "inspector"
DEMO_INSPECTOR_2 = "inspector2"
DEMO_PASSWORD_ADMIN = "admin-demo-123"
DEMO_PASSWORD_INSPECTOR = "inspector-demo-123"

# (code, kind, title, parent_code, value_type, is_mandatory)
PEDAGOGICAL_TREE = [
    ("PED", NodeType.BRANCH, "التخطيط البيداغوجي", None, ValueType.NONE, False),
    ("PED-DOC", NodeType.DESCRIPTION, "الوثائق البيداغوجية", "PED", ValueType.NONE, False),
    ("PED-DOC-1", NodeType.CHECKLIST_ITEM, "توفر مخطط التكوين", "PED-DOC", ValueType.NONE, False),
    ("PED-DOC-2", NodeType.CHECKLIST_ITEM, "توفر المذكرات البيداغوجية", "PED-DOC", ValueType.NONE, True),
    ("PED-PROG", NodeType.BRANCH, "تنفيذ البرنامج", "PED", ValueType.NONE, False),
    ("PED-PROG-1", NodeType.CHECKLIST_ITEM, "احترام الحجم الساعي", "PED-PROG", ValueType.NONE, False),
    ("PED-PROG-2", NodeType.DESCRIPTION, "نسبة تقدم البرنامج", "PED-PROG", ValueType.NUMBER, False),
    ("ENT", NodeType.BRANCH, "الدخول التكويني", None, ValueType.NONE, False),
    ("ENT-1", NodeType.CHECKLIST_ITEM, "قوائم المتدربين محدّثة", "ENT", ValueType.NONE, False),
    ("ENT-2", NodeType.DESCRIPTION, "تاريخ آخر تحديث للقوائم", "ENT", ValueType.DATE, False),
]

EQUIPMENT_TREE = [
    ("EQP", NodeType.BRANCH, "متابعة التجهيزات", None, ValueType.NONE, False),
    ("EQP-1", NodeType.CHECKLIST_ITEM, "صلاحية التجهيزات", "EQP", ValueType.NONE, False),
    ("EQP-2", NodeType.CHECKLIST_ITEM, "سجل الصيانة", "EQP", ValueType.NONE, True),
    ("EQP-3", NodeType.DESCRIPTION, "عدد التجهيزات المعطلة", "EQP", ValueType.NUMBER, False),
]


class Command(BaseCommand):
    help = "ينشئ بيانات تجريبية قابلة لإعادة التشغيل دون إتلاف البيانات."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="يحذف بيانات العرض التجريبية فقط قبل إعادة إنشائها.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            self._reset()

        admin = self._user(DEMO_ADMIN, DEMO_PASSWORD_ADMIN, Role.ADMIN, "مدير العرض")
        inspector = self._user(
            DEMO_INSPECTOR, DEMO_PASSWORD_INSPECTOR, Role.INSPECTOR, "مفتش العرض"
        )
        self._user(
            DEMO_INSPECTOR_2, DEMO_PASSWORD_INSPECTOR, Role.INSPECTOR, "مفتش ثانٍ"
        )

        institutions = self._institutions()
        pedagogical = self._reference(
            admin, "مرجع التفتيش البيداغوجي", PEDAGOGICAL_TREE
        )
        equipment = self._reference(admin, "مرجع متابعة التجهيزات", EQUIPMENT_TREE)

        self._guides(admin, pedagogical, equipment)
        self._visits(inspector, institutions, pedagogical, equipment)

        self.stdout.write(self.style.SUCCESS("تم تجهيز بيانات العرض التجريبية."))
        self.stdout.write("  admin / admin-demo-123")
        self.stdout.write("  inspector / inspector-demo-123")
        self.stdout.write("  inspector2 / inspector-demo-123")

    # -- helpers -----------------------------------------------------------
    def _reset(self):
        Visit = self._visit_model()
        visits = Visit.objects.filter(
            inspector__username__in=[DEMO_INSPECTOR, DEMO_INSPECTOR_2]
        )
        visits.delete()
        Guide.objects.filter(title__startswith="[عرض]").delete()
        from catalog.models import Reference

        Reference.objects.filter(
            title__in=["مرجع التفتيش البيداغوجي", "مرجع متابعة التجهيزات"]
        ).delete()
        Institution.objects.filter(code__startswith="DEMO-").delete()
        self.stdout.write("تم حذف بيانات العرض السابقة.")

    @staticmethod
    def _visit_model():
        from visits.models import Visit

        return Visit

    def _user(self, username, password, role, display_name):
        user = User.objects.filter(username=username).first()
        if user is None:
            user = User.objects.create_user(
                username=username,
                password=password,
                role=role,
                display_name=display_name,
            )
            self.stdout.write(f"  + مستخدم: {username}")
        return user

    def _institutions(self):
        specs = [
            ("DEMO-01", "مركز التكوين المهني - النور", InstitutionKind.TRAINING_CENTER, "الجزائر"),
            ("DEMO-02", "معهد التمهين - الفجر", InstitutionKind.APPRENTICESHIP, "البليدة"),
            ("DEMO-03", "المدرسة المهنية - الأمل", InstitutionKind.VOCATIONAL_SCHOOL, "قسنطينة"),
        ]
        result = []
        for code, name, kind, location in specs:
            institution, _ = Institution.objects.get_or_create(
                code=code,
                defaults={"name": name, "kind": kind, "location": location},
            )
            result.append(institution)
        return result

    def _reference(self, admin, title, tree):
        from catalog.models import Reference

        existing = Reference.objects.filter(title=title, visibility=ReferenceVisibility.SHARED).first()
        if existing and existing.nodes.exists():
            return existing
        reference = existing or create_reference(
            actor=admin, title=title, visibility=ReferenceVisibility.SHARED
        )
        by_code = {}
        for code, kind, node_title, parent_code, value_type, mandatory in tree:
            by_code[code] = add_node(
                reference=reference,
                node_type=kind,
                code=code,
                title=node_title,
                parent=by_code.get(parent_code),
                value_type=value_type,
                is_mandatory=mandatory,
            )
        self.stdout.write(f"  + مرجع: {title} ({len(tree)} عنصر)")
        return reference

    def _guides(self, admin, pedagogical, equipment):
        specs = [
            ("[عرض] التخطيط البيداغوجي الأساسي", pedagogical, ["PED-DOC", "PED-PROG-1"]),
            ("[عرض] متابعة التجهيزات السريعة", equipment, ["EQP-1", "EQP-2"]),
        ]
        for title, reference, codes in specs:
            guide, created = Guide.objects.get_or_create(
                reference=reference,
                title=title,
                defaults={
                    "description": "دليل تجريبي اختياري، لا يفرض أي التزام.",
                    "created_by": admin,
                    "status": GuideStatus.PUBLISHED,
                },
            )
            if created:
                for index, code in enumerate(codes):
                    node = reference.nodes.get(code=code)
                    GuideTarget.objects.create(
                        guide=guide,
                        code=code,
                        title_snapshot=node.title,
                        node_type_snapshot=node.node_type,
                        position=(index + 1) * 10,
                    )
                self.stdout.write(f"  + دليل: {title}")

    def _visits(self, inspector, institutions, pedagogical, equipment):
        Visit = self._visit_model()
        if Visit.objects.filter(inspector=inspector).exists():
            self.stdout.write("  = زيارات العرض موجودة مسبقًا.")
            return

        # 1. A draft under construction with a hand-picked scope.
        draft = create_visit(
            actor=inspector,
            institution=institutions[0],
            visit_date=datetime.date(2026, 3, 2),
            title="زيارة متابعة بيداغوجية",
            reference=pedagogical,
            notes="زيارة دورية.",
        )
        select_node(visit=draft, node=draft.nodes.get(code="PED-DOC-1"))
        select_node(visit=draft, node=draft.nodes.get(code="PED-PROG-1"))

        # 2. A draft with no reference at all: local authoring only.
        local = create_visit(
            actor=inspector,
            institution=institutions[1],
            visit_date=datetime.date(2026, 3, 9),
            title="زيارة استكشافية",
            notes="زيارة بلا مرجع، محتوى محلي فقط.",
        )
        from visits.services import add_local_node

        branch = add_local_node(
            visit=local, node_type=NodeType.BRANCH, title="ملاحظات ميدانية"
        )
        add_local_node(
            visit=local,
            node_type=NodeType.CHECKLIST_ITEM,
            title="تنظيف الورشات",
            parent=branch,
        )

        # 3. A completed visit, immutable afterwards.
        completed = create_visit(
            actor=inspector,
            institution=institutions[2],
            visit_date=datetime.date(2026, 2, 10),
            title="زيارة مكتملة",
            reference=equipment,
        )
        from visits.services import complete_visit

        for code in ["EQP-1", "EQP-2"]:
            node = completed.nodes.get(code=code)
            select_node(visit=completed, node=node)
            record_result(
                visit=completed,
                node=node,
                result=ItemResult.MATCH,
                observation="مطابق للشروط.",
            )
        complete_visit(visit=completed, actor=inspector)
        self.stdout.write("  + ثلاث زيارات تجريبية (مسودة، محلية، مكتملة).")