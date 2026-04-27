"""Unit tests for the new features added in the geocube contribution PRs.

Like ``test_wrapper.py``, these tests are pure-Python (no QGIS install, no
server).  PyQGIS symbols and ADBC driver are stubbed here.  Each test class
exercises the actual production code via ``object.__new__`` to bypass the
``__init__`` that requires a live connection.

Covered:
* GizmoSqlProvider._sql_literal  (write-support PR)
* GizmoSqlProvider._qualified_table  (write-support PR)
* GizmoSqlFeatureIterator._advance_batch  (arrow-batch-streaming PR)
* GizmoSqlFeatureIterator.rewind  (arrow-batch-streaming PR — forward-only)
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Stubs — must be installed before any qgizmosql import
# ---------------------------------------------------------------------------

def _install_stubs() -> None:  # noqa: C901  (complexity is unavoidable here)
    """Install minimal stubs for PyQGIS, adbc, and the plugin toolbelt."""

    # ---- qgis.PyQt.QtCore ------------------------------------------------

    class _QMetaType:
        class Type:
            Bool = 1
            Int = 2
            Double = 6
            QString = 10
            QDate = 14
            QTime = 15
            QDateTime = 16

    class _QVariant:
        # Legacy Qt4/QGIS-3 attribute-style access (QVariant.Int, etc.)
        Int = 2
        Bool = 1
        Double = 6
        String = 10
        Date = 14
        Time = 15
        DateTime = 16

        class Type:
            pass

    class _QDate:
        pass

    class _QTime:
        pass

    class _QDateTime:
        pass

    # ---- qgis.core — enums -----------------------------------------------

    class _MessageLevel:
        NoLevel = 0
        Info = 1
        Warning = 2
        Critical = 3
        Success = 4

    class _Qgis:
        MessageLevel = _MessageLevel
        QGIS_VERSION_INT = 40000

    class _WkbType:
        NoGeometry = 0
        Unknown = 1
        Point = 1001
        LineString = 1002
        Polygon = 1003
        MultiPoint = 1004
        MultiLineString = 1005
        MultiPolygon = 1006

    class _QgsWkbTypes:
        Type = _WkbType

    # ---- qgis.core — CRS / geometry / fields / features ------------------

    class _QgsCoordinateReferenceSystem:
        _epsg = None

        @staticmethod
        def fromEpsgId(epsg_id):
            obj = _QgsCoordinateReferenceSystem()
            obj._epsg = epsg_id
            return obj

        def isValid(self):
            return self._epsg is not None

    class _QgsCoordinateTransform:
        pass

    class _QgsCsException(Exception):
        pass

    class _QgsRectangle:
        def __init__(self, *args):
            self._null = len(args) == 0

        def isNull(self):
            return self._null

    class _QgsGeometry:
        def fromWkb(self, wkb):
            pass

    class _QgsField:
        def __init__(self, name="", field_type=None):
            self._name = name
            self._type = field_type

        def name(self):
            return self._name

        def type(self):
            return self._type

    class _QgsFields:
        def __init__(self):
            self._fields = []

        def append(self, field):
            self._fields.append(field)

        def count(self):
            return len(self._fields)

        def field(self, idx):
            return self._fields[idx]

        def __len__(self):
            return len(self._fields)

        def __iter__(self):
            return iter(self._fields)

        def __getitem__(self, idx):
            return self._fields[idx]

    class _QgsFeature:
        def setValid(self, v):
            pass

        def setFields(self, f):
            pass

        def setId(self, i):
            pass

        def setGeometry(self, g):
            pass

        def setAttribute(self, idx, val):
            pass

        def setAttributes(self, attrs):
            pass

    class _QgsFeatureRequest:
        class Flag:
            SubsetOfAttributes = 1
            NoGeometry = 2

        class FilterType:
            FilterNone = 0
            FilterFid = 1
            FilterFids = 2
            FilterExpression = 3

    class _QgsExpression:
        pass

    class _QgsExpressionContext:
        def appendScope(self, s):
            pass

        def setFields(self, f):
            pass

    class _QgsExpressionContextUtils:
        @staticmethod
        def globalScope():
            return None

        @staticmethod
        def projectScope(project):
            return None

    class _QgsProject:
        @staticmethod
        def instance():
            return None

    class _QgsFeatureIterator:
        def __init__(self, iterator):
            pass

    # ---- qgis.core — provider base classes --------------------------------

    class _QgsDataProvider:
        class ProviderOptions:
            pass

        class ReadFlags:
            pass

    class _Capabilities(int):
        def __new__(cls, v=0):
            return int.__new__(cls, v)

        def __or__(self, other):
            return _Capabilities(int(self) | int(other))

    class _Capability:
        CreateSpatialIndex = 1
        SelectAtId = 2
        AddFeatures = 4
        DeleteFeatures = 8
        ChangeAttributeValues = 16
        ChangeGeometries = 32

    class _QgsVectorDataProvider:
        Capabilities = _Capabilities
        Capability = _Capability

        class Flags:
            pass

        def __init__(self, *args, **kwargs):
            pass

        def tr(self, s):
            return s

        def reloadData(self):
            pass

    class _QgsAbstractFeatureIterator:
        def __init__(self, request):
            pass

        def filterRectToSourceCrs(self, transform):
            r = _QgsRectangle()
            r._null = True
            return r

        def geometryToDestinationCrs(self, f, transform):
            pass

        def nextFeature(self, f):
            return self.fetchFeature(f)

    class _QgsAbstractFeatureSource:
        def __init__(self):
            pass

    # ---- qgis.core — auth (used by gizmosql_wrapper) ----------------------

    class _QgsApplication:
        @staticmethod
        def authManager():
            return None

    class _QgsAuthMethodConfig:
        def __init__(self):
            self._cfg = {}

        def config(self, key):
            return self._cfg.get(key)

    # ---- wire up qgis modules ---------------------------------------------

    qgis_mod = types.ModuleType("qgis")
    qgis_core_mod = types.ModuleType("qgis.core")
    qgis_pyqt_mod = types.ModuleType("qgis.PyQt")
    qgis_pyqt_qtcore_mod = types.ModuleType("qgis.PyQt.QtCore")

    qgis_core_mod.Qgis = _Qgis
    qgis_core_mod.QgsCoordinateReferenceSystem = _QgsCoordinateReferenceSystem
    qgis_core_mod.QgsCoordinateTransform = _QgsCoordinateTransform
    qgis_core_mod.QgsCsException = _QgsCsException
    qgis_core_mod.QgsDataProvider = _QgsDataProvider
    qgis_core_mod.QgsExpression = _QgsExpression
    qgis_core_mod.QgsExpressionContext = _QgsExpressionContext
    qgis_core_mod.QgsExpressionContextUtils = _QgsExpressionContextUtils
    qgis_core_mod.QgsFeature = _QgsFeature
    qgis_core_mod.QgsFeatureIterator = _QgsFeatureIterator
    qgis_core_mod.QgsFeatureRequest = _QgsFeatureRequest
    qgis_core_mod.QgsField = _QgsField
    qgis_core_mod.QgsFields = _QgsFields
    qgis_core_mod.QgsGeometry = _QgsGeometry
    qgis_core_mod.QgsProject = _QgsProject
    qgis_core_mod.QgsRectangle = _QgsRectangle
    qgis_core_mod.QgsVectorDataProvider = _QgsVectorDataProvider
    qgis_core_mod.QgsWkbTypes = _QgsWkbTypes
    qgis_core_mod.QgsAbstractFeatureIterator = _QgsAbstractFeatureIterator
    qgis_core_mod.QgsAbstractFeatureSource = _QgsAbstractFeatureSource
    qgis_core_mod.QgsApplication = _QgsApplication
    qgis_core_mod.QgsAuthMethodConfig = _QgsAuthMethodConfig

    qgis_pyqt_qtcore_mod.QMetaType = _QMetaType
    qgis_pyqt_qtcore_mod.QVariant = _QVariant
    qgis_pyqt_qtcore_mod.QDate = _QDate
    qgis_pyqt_qtcore_mod.QTime = _QTime
    qgis_pyqt_qtcore_mod.QDateTime = _QDateTime

    sys.modules.setdefault("qgis", qgis_mod)
    sys.modules["qgis.core"] = qgis_core_mod
    sys.modules.setdefault("qgis.PyQt", qgis_pyqt_mod)
    sys.modules["qgis.PyQt.QtCore"] = qgis_pyqt_qtcore_mod

    # ---- toolbelt stubs ---------------------------------------------------

    toolbelt_mod = types.ModuleType("qgizmosql.toolbelt")
    log_handler_mod = types.ModuleType("qgizmosql.toolbelt.log_handler")
    preferences_mod = types.ModuleType("qgizmosql.toolbelt.preferences")

    class _PlgLogger:
        @staticmethod
        def log(**kwargs):
            pass

        def __call__(self):
            return self

    class _PlgSettings:
        debug_mode = False

    class _PlgOptionsManager:
        @staticmethod
        def get_plg_settings():
            return _PlgSettings()

    log_handler_mod.PlgLogger = _PlgLogger
    preferences_mod.PlgOptionsManager = _PlgOptionsManager
    preferences_mod.PlgSettings = _PlgSettings

    sys.modules.setdefault("qgizmosql.toolbelt", toolbelt_mod)
    sys.modules["qgizmosql.toolbelt.log_handler"] = log_handler_mod
    sys.modules["qgizmosql.toolbelt.preferences"] = preferences_mod

    # ---- adbc stubs -------------------------------------------------------

    adbc_pkg = types.ModuleType("adbc_driver_gizmosql")
    adbc_dbapi = types.ModuleType("adbc_driver_gizmosql.dbapi")
    adbc_dbapi.connect = lambda *a, **kw: None
    adbc_pkg.dbapi = adbc_dbapi
    sys.modules.setdefault("adbc_driver_gizmosql", adbc_pkg)
    sys.modules.setdefault("adbc_driver_gizmosql.dbapi", adbc_dbapi)


_install_stubs()

# Now safe to import production code.
from qgizmosql.provider.gizmosql_provider import GizmoSqlProvider  # noqa: E402
from qgizmosql.provider.gizmosql_feature_iterator import (  # noqa: E402
    GizmoSqlFeatureIterator,
)

# Feature-detection flags — used to skip tests for features that aren't on
# the currently checked-out branch (each PR lives on its own branch).
_has_write_support = hasattr(GizmoSqlProvider, "_sql_literal")
_has_arrow_streaming = hasattr(GizmoSqlFeatureIterator, "_advance_batch")


# ---------------------------------------------------------------------------
# Helper to create bare instances that bypass __init__
# ---------------------------------------------------------------------------

def _bare_provider(**attrs):
    """Return a GizmoSqlProvider instance without calling its __init__."""
    obj = object.__new__(GizmoSqlProvider)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def _bare_iterator(**attrs):
    """Return a GizmoSqlFeatureIterator instance without calling its __init__."""
    obj = object.__new__(GizmoSqlFeatureIterator)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# ===========================================================================
# Tests: GizmoSqlProvider._sql_literal
# ===========================================================================

@unittest.skipUnless(_has_write_support, "write-support not on this branch")
class TestSqlLiteral(unittest.TestCase):
    """Tests for GizmoSqlProvider._sql_literal — SQL injection safety."""

    def _lit(self, value):
        return GizmoSqlProvider._sql_literal(value)

    # ---- NULL / None -------------------------------------------------------

    def test_none_becomes_null(self):
        self.assertEqual(self._lit(None), "NULL")

    # ---- booleans ----------------------------------------------------------

    def test_true_becomes_TRUE(self):
        self.assertEqual(self._lit(True), "TRUE")

    def test_false_becomes_FALSE(self):
        self.assertEqual(self._lit(False), "FALSE")

    def test_bool_takes_priority_over_int(self):
        # In Python, bool is a subclass of int; make sure bool wins.
        self.assertNotEqual(self._lit(True), "1")
        self.assertNotEqual(self._lit(False), "0")

    # ---- numbers -----------------------------------------------------------

    def test_integer(self):
        self.assertEqual(self._lit(42), "42")

    def test_negative_integer(self):
        self.assertEqual(self._lit(-7), "-7")

    def test_float(self):
        self.assertEqual(self._lit(3.14), "3.14")

    def test_zero(self):
        self.assertEqual(self._lit(0), "0")

    # ---- strings -----------------------------------------------------------

    def test_plain_string(self):
        self.assertEqual(self._lit("hello"), "'hello'")

    def test_empty_string(self):
        self.assertEqual(self._lit(""), "''")

    def test_string_with_single_quote_is_escaped(self):
        """Single quotes inside a string must be doubled — SQL standard."""
        result = self._lit("O'Brien")
        self.assertEqual(result, "'O''Brien'")

    def test_double_single_quote_in_string(self):
        result = self._lit("it's a ''test''")
        # Each ' → '' so the two '' become ''''
        self.assertEqual(result, "'it''s a ''''test'''''")

    def test_string_that_looks_like_sql(self):
        """Strings containing SQL keywords must stay as literals."""
        result = self._lit("'; DROP TABLE users; --")
        # Must be wrapped in single quotes with all interior quotes doubled.
        self.assertTrue(result.startswith("'"))
        self.assertTrue(result.endswith("'"))
        # The DROP TABLE text must be inside the outer quotes (harmless literal).
        self.assertIn("DROP TABLE", result)
        # No bare single quote before DROP (that would allow injection).
        # Verify by checking round-trip: remove outer quotes and un-double.
        inner = result[1:-1].replace("''", "'")
        self.assertEqual(inner, "'; DROP TABLE users; --")

    def test_string_with_only_single_quotes(self):
        self.assertEqual(self._lit("''"), "''''''")

    # ---- date/datetime (passed as strings from QGIS) ----------------------

    def test_date_string(self):
        self.assertEqual(self._lit("2024-01-15"), "'2024-01-15'")

    def test_datetime_string(self):
        result = self._lit("2024-01-15 12:34:56")
        self.assertEqual(result, "'2024-01-15 12:34:56'")


# ===========================================================================
# Tests: GizmoSqlProvider._qualified_table
# ===========================================================================

@unittest.skipUnless(_has_write_support, "write-support not on this branch")
class TestQualifiedTable(unittest.TestCase):
    """Tests for GizmoSqlProvider._qualified_table."""

    def test_schema_and_table(self):
        prov = _bare_provider(_schema="myschema", _table="cities")
        self.assertEqual(prov._qualified_table(), '"myschema"."cities"')

    def test_no_schema_defaults_to_main(self):
        prov = _bare_provider(_schema=None, _table="rivers")
        self.assertEqual(prov._qualified_table(), '"main"."rivers"')

    def test_empty_schema_defaults_to_main(self):
        prov = _bare_provider(_schema="", _table="lakes")
        self.assertEqual(prov._qualified_table(), '"main"."lakes"')

    def test_table_with_spaces_keeps_quotes(self):
        prov = _bare_provider(_schema="public", _table="my table")
        result = prov._qualified_table()
        # Both identifiers must be double-quoted.
        self.assertEqual(result, '"public"."my table"')


# ===========================================================================
# Tests: GizmoSqlFeatureIterator._advance_batch  (Arrow streaming)
# ===========================================================================

@unittest.skipUnless(_has_arrow_streaming, "arrow-batch-streaming not on this branch")
class TestAdvanceBatch(unittest.TestCase):
    """Tests for the Arrow record-batch streaming helper."""

    def _make_iterator(self, reader):
        return _bare_iterator(
            _batch_reader=reader,
            _current_batch=None,
            _batch_row_index=0,
        )

    def test_returns_true_when_batch_available(self):
        mock_reader = MagicMock()
        mock_batch = MagicMock()
        mock_reader.read_next_batch.return_value = mock_batch

        it = self._make_iterator(mock_reader)
        result = it._advance_batch()

        self.assertTrue(result)

    def test_sets_current_batch_on_success(self):
        mock_reader = MagicMock()
        mock_batch = MagicMock()
        mock_reader.read_next_batch.return_value = mock_batch

        it = self._make_iterator(mock_reader)
        it._advance_batch()

        self.assertIs(it._current_batch, mock_batch)

    def test_resets_batch_row_index_to_zero_on_success(self):
        mock_reader = MagicMock()
        mock_reader.read_next_batch.return_value = MagicMock()

        it = self._make_iterator(mock_reader)
        it._batch_row_index = 99          # dirty from previous batch
        it._advance_batch()

        self.assertEqual(it._batch_row_index, 0)

    def test_returns_false_when_exhausted(self):
        mock_reader = MagicMock()
        mock_reader.read_next_batch.side_effect = StopIteration()

        it = self._make_iterator(mock_reader)
        result = it._advance_batch()

        self.assertFalse(result)

    def test_sets_current_batch_to_none_when_exhausted(self):
        mock_reader = MagicMock()
        mock_reader.read_next_batch.side_effect = StopIteration()

        it = self._make_iterator(mock_reader)
        it._current_batch = MagicMock()  # had a previous batch
        it._advance_batch()

        self.assertIsNone(it._current_batch)

    def test_calls_read_next_batch_exactly_once(self):
        mock_reader = MagicMock()
        mock_reader.read_next_batch.return_value = MagicMock()

        it = self._make_iterator(mock_reader)
        it._advance_batch()

        mock_reader.read_next_batch.assert_called_once()


# ===========================================================================
# Tests: GizmoSqlFeatureIterator.rewind  (Arrow version returns False)
# ===========================================================================

@unittest.skipUnless(_has_arrow_streaming, "arrow-batch-streaming not on this branch")
class TestArrowRewind(unittest.TestCase):
    """Arrow record-batch readers are forward-only — rewind() must return False."""

    def test_rewind_returns_false_when_index_is_non_negative(self):
        it = _bare_iterator(_index=0)
        self.assertFalse(it.rewind())

    def test_rewind_returns_false_mid_stream(self):
        it = _bare_iterator(_index=5)
        self.assertFalse(it.rewind())

    def test_rewind_returns_false_when_not_yet_started(self):
        # _index == 0 means we haven't fetched anything yet.
        it = _bare_iterator(_index=0)
        self.assertFalse(it.rewind())

    def test_close_sets_index_negative_and_rewind_then_returns_false(self):
        """After close(), _index is -1; rewind must still behave predictably."""
        it = _bare_iterator(
            _index=3,
            _batch_reader=None,
            _current_batch=None,
        )
        it.close()
        # _index is now -1; rewind returns False (same as original upstream
        # guard: ``if self._index < 0: return False``).
        self.assertFalse(it.rewind())


if __name__ == "__main__":
    unittest.main()
