import unittest


_IMPORT_ERROR = None
appmod = None
try:
    from flask import Flask
    from sqlalchemy import func

    from app import app as appmod
    from app.db import db, Apps, Titles
except ModuleNotFoundError as exc:
    _IMPORT_ERROR = exc


class LibraryBaseRowTests(unittest.TestCase):
    """The library listing shows one BASE row per title."""

    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency for library base row tests: {_IMPORT_ERROR}")

    def setUp(self):
        self.flask_app = Flask(__name__)
        self.flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.flask_app)
        self.ctx = self.flask_app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _title(self, title_id, bases, extra=()):
        title = Titles(title_id=title_id)
        db.session.add(title)
        db.session.flush()
        for version, owned in bases:
            db.session.add(Apps(title_id=title.id, app_id=title_id, app_version=str(version),
                                app_type='BASE', owned=owned))
        for app_id, version, app_type, owned in extra:
            db.session.add(Apps(title_id=title.id, app_id=app_id, app_version=str(version),
                                app_type=app_type, owned=owned))
        db.session.commit()

    def _base_rows(self):
        version_expr = func.coalesce(Apps.app_version_num, 0)
        sub = appmod._library_base_choice_subquery(version_expr)
        rows = (
            db.session.query(Titles.title_id, Apps.app_version, Apps.owned)
            .join(Titles, Apps.title_id == Titles.id)
            .outerjoin(sub, sub.c.title_fk == Titles.id)
            .filter(appmod._library_base_row_condition(sub, version_expr))
            .order_by(Titles.title_id)
            .all()
        )
        return [(r.title_id, r.app_version, bool(r.owned)) for r in rows]

    def test_owned_later_base_hides_unowned_v0(self):
        # titledb lists a v0 base; the owned dump is a v65536 base.
        self._title('0100000000060000', [(0, False), (65536, True)],
                    extra=[('0100000000060800', 65536, 'UPDATE', True)])
        self.assertEqual(self._base_rows(), [('0100000000060000', '65536', True)])

    def test_owned_v0_hides_unowned_later_base(self):
        self._title('0100000000010000', [(0, True), (65536, False)])
        self.assertEqual(self._base_rows(), [('0100000000010000', '0', True)])

    def test_highest_owned_base_wins(self):
        self._title('0100000000020000', [(0, True), (65536, True), (131072, False)])
        self.assertEqual(self._base_rows(), [('0100000000020000', '65536', True)])

    def test_unowned_title_shows_highest_base(self):
        self._title('0100000000030000', [(0, False), (65536, False)])
        self.assertEqual(self._base_rows(), [('0100000000030000', '65536', False)])

    def test_single_base_unchanged(self):
        self._title('0100000000040000', [(0, True)])
        self._title('0100000000050000', [(0, False)])
        self.assertEqual(self._base_rows(), [
            ('0100000000040000', '0', True),
            ('0100000000050000', '0', False),
        ])


if __name__ == '__main__':
    unittest.main()
