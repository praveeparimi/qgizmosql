from __future__ import annotations

import weakref
from typing import Any, Optional

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsDataProvider,
    QgsFeature,
    QgsFeatureIterator,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsRectangle,
    QgsVectorDataProvider,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType

from qgizmosql.provider import gizmosql_feature_iterator, gizmosql_feature_source
from qgizmosql.provider.gizmosql_wrapper import GizmoSqlTools
from qgizmosql.provider.mappings import (
    deprecate_mapping_duckdb_qgis_type,
    mapping_duckdb_qgis_geometry,
    mapping_duckdb_qgis_type,
)
from qgizmosql.toolbelt.log_handler import PlgLogger


class GizmoSqlProvider(QgsVectorDataProvider):
    """QGIS vector data provider backed by a GizmoSQL server.

    The wire protocol is Arrow Flight SQL, accessed through the
    ``adbc-driver-gizmosql`` Python client. Server executes DuckDB SQL —
    including the ``spatial`` extension — so spatial-type introspection
    still uses ``information_schema`` and ``ST_*`` functions.
    """

    def __init__(
        self,
        uri: str = "",
        providerOptions: QgsDataProvider.ProviderOptions = None,
        flags: QgsDataProvider.ReadFlags = None,
    ):
        super().__init__(uri)
        providerOptions = providerOptions or QgsDataProvider.ProviderOptions()
        flags = flags if flags is not None else QgsDataProvider.ReadFlags()

        self.wrapper = GizmoSqlTools()
        self._is_valid = False
        self._uri = uri
        self._wkb_type: Optional[QgsWkbTypes.Type] = None
        self._extent: Optional[QgsRectangle] = None
        self._column_geom: Optional[str] = None
        self._fields: Optional[QgsFields] = None
        self._feature_count: Optional[int] = None
        self._primary_key: Optional[int] = None
        self.filter_where_clause: Optional[str] = None
        self._con: Any = None

        try:
            (
                self._conn_config,
                self._table,
                self._epsg,
                self._sql,
                self._schema,
            ) = self.wrapper.parse_uri(uri)
        except ValueError as exc:
            self._is_valid = False
            PlgLogger.log(message=str(exc), log_level=Qgis.MessageLevel.Critical, push=True)
            return

        if self._sql:
            # encodeUri may have escaped double-quotes; undo that for execution.
            self._sql = self._sql.replace('\\"', '"')

        self._crs = (
            QgsCoordinateReferenceSystem.fromEpsgId(int(self._epsg))
            if self._epsg
            else QgsCoordinateReferenceSystem()
        )

        try:
            self.connect_database()
        except Exception as exc:
            self._is_valid = False
            PlgLogger.log(
                message=f"Could not connect to GizmoSQL: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=True,
            )
            return

        if self._sql and not self._table:
            if not self.test_sql_query():
                return
            self._from_clause = f"({self._sql})"
        else:
            schema = self._schema or "main"
            self._from_clause = f'"{schema}"."{self._table}"'

        self.get_geometry_column()

        self._provider_options = providerOptions
        self._flags = flags
        self._is_valid = True
        weakref.finalize(self, self.disconnect_database)

        if Qgis.QGIS_VERSION_INT < 33800:
            self.mapping_field_type = deprecate_mapping_duckdb_qgis_type
        else:
            self.mapping_field_type = mapping_duckdb_qgis_type

    # -- provider identity -----------------------------------------------------

    @classmethod
    def providerKey(cls) -> str:
        return "gizmosql"

    @classmethod
    def description(cls) -> str:
        return "GizmoSQL"

    @classmethod
    def createProvider(cls, uri, providerOptions, flags=QgsDataProvider.ReadFlags()):
        return GizmoSqlProvider(uri, providerOptions, flags)

    def name(self) -> str:
        return self.providerKey()

    def storageType(self) -> str:
        return "GizmoSQL Arrow Flight SQL server"

    def isValid(self) -> bool:
        return self._is_valid

    def capabilities(self) -> QgsVectorDataProvider.Capabilities:
        base = (
            QgsVectorDataProvider.Capability.CreateSpatialIndex
            | QgsVectorDataProvider.Capability.SelectAtId
        )
        # Write capabilities are only available for named tables, not custom SQL.
        if self._is_valid and not self._sql:
            base |= (
                QgsVectorDataProvider.Capability.AddFeatures
                | QgsVectorDataProvider.Capability.DeleteFeatures
                | QgsVectorDataProvider.Capability.ChangeAttributeValues
                | QgsVectorDataProvider.Capability.ChangeGeometries
            )
        return base

    # -- connection ------------------------------------------------------------

    def connect_database(self) -> None:
        self.wrapper.conn_config = self._conn_config
        self._con = self.wrapper.connect()

    def disconnect_database(self) -> None:
        if self._con is not None and self.isValid():
            try:
                self._con.close()
            except Exception:
                pass
            self._con = None

    def con(self) -> Optional[Any]:
        """Return a raw ADBC cursor for callers (e.g. the feature iterator)."""
        if not self._is_valid or self._con is None:
            return None
        return self._con.cursor()

    def test_sql_query(self) -> bool:
        if not self._sql:
            return True
        try:
            # Probe with LIMIT 0 — cheapest possible parse/plan check.
            with self._con.cursor() as cur:
                cur.execute(f"SELECT * FROM ({self._sql}) LIMIT 0")
            return True
        except Exception as exc:
            PlgLogger.log(
                self.tr(f"The SQL query is invalid: {exc}"),
                log_level=Qgis.MessageLevel.Critical,
                duration=15,
                push=True,
            )
            return False

    # -- feature metadata ------------------------------------------------------

    def featureCount(self) -> int:
        if self._feature_count is None:
            if not self._is_valid:
                self._feature_count = 0
            else:
                if self.subsetString():
                    row = self._con.sql(
                        f"select count(*) from {self._from_clause} WHERE {self.subsetString()}"
                    ).fetchone()
                else:
                    row = self._con.sql(
                        f"select count(*) from {self._from_clause}"
                    ).fetchone()
                self._feature_count = row[0] if row else 0
        return self._feature_count

    def wkbType(self) -> QgsWkbTypes:
        if not self._column_geom:
            return QgsWkbTypes.Type.NoGeometry
        if self._wkb_type is None:
            if not self._is_valid:
                self._wkb_type = QgsWkbTypes.Type.Unknown
            else:
                row = self._con.sql(
                    f"select st_geometrytype({self._column_geom}) from {self._from_clause} limit 1"
                ).fetchone()
                str_geom = row[0] if row else None
                if str_geom in mapping_duckdb_qgis_geometry:
                    self._wkb_type = mapping_duckdb_qgis_geometry[str_geom]
                else:
                    PlgLogger.log(
                        self.tr(f"Geometry type {str_geom} not supported"),
                        log_level=Qgis.MessageLevel.Critical,
                        duration=15,
                        push=True,
                    )
                    self._wkb_type = QgsWkbTypes.Type.Unknown
        return self._wkb_type

    def extent(self) -> QgsRectangle:
        if self._extent is None:
            if not self._is_valid or not self._column_geom:
                self._extent = QgsRectangle()
                PlgLogger.log(
                    message="Table without geometry, can not compute an extent",
                    log_level=Qgis.MessageLevel.Success,
                    push=False,
                )
            else:
                extent_bounds = self._con.sql(
                    query=f"select min(st_xmin({self._column_geom})), "
                    f"min(st_ymin({self._column_geom})), "
                    f"max(st_xmax({self._column_geom})), "
                    f"max(st_ymax({self._column_geom})) "
                    f"from {self._from_clause}"
                ).fetchone()
                self._extent = QgsRectangle(*extent_bounds)
                PlgLogger.log(
                    message="Extent calculated for {}: xmin={}, xmax={}, ymin={}, ymax={}".format(
                        self._table, *extent_bounds
                    ),
                    log_level=Qgis.MessageLevel.Success,
                )
        return self._extent

    def updateExtents(self) -> None:
        if self._extent is not None:
            self._extent.setMinimal()

    def get_geometry_column(self) -> Optional[str]:
        if self._column_geom is not None:
            return self._column_geom
        if not self._sql:
            schema = self._schema or "main"
            row = self._con.sql(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{self._table}' AND table_schema = '{schema}' "
                "AND data_type = 'GEOMETRY'"
            ).fetchone()
            if row:
                self._column_geom = row[0]
        else:
            # Use DuckDB's DESCRIBE to introspect an ad-hoc query's output types.
            rows = self._con.sql(f"DESCRIBE {self._sql}").fetchall()
            for r in rows:
                col_name, col_type = r[0], r[1]
                if col_type == "GEOMETRY":
                    self._column_geom = col_name
                    break
        return self._column_geom

    def primary_key(self) -> int:
        if self._primary_key is not None:
            return self._primary_key
        if self._sql:
            self._primary_key = -1
            return self._primary_key
        schema = self._schema or "main"
        # information_schema-only query — portable across DuckDB versions and
        # avoids relying on the ``duckdb_constraints()`` table function, which
        # is server-internal.
        row = self._con.sql(
            "SELECT kcu.ordinal_position - 1 "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "  AND tc.table_schema = kcu.table_schema "
            "  AND tc.table_name = kcu.table_name "
            f"WHERE tc.constraint_type = 'PRIMARY KEY' "
            f"  AND tc.table_name = '{self._table}' "
            f"  AND tc.table_schema = '{schema}' "
            "ORDER BY kcu.ordinal_position"
        ).fetchone()
        self._primary_key = int(row[0]) if row else -1
        return self._primary_key

    def fields(self) -> QgsFields:
        if self._fields is not None:
            return self._fields
        self._fields = QgsFields()
        if not self._is_valid:
            return self._fields

        if not self._sql:
            schema = self._schema or "main"
            field_info = self._con.sql(
                "select column_name, data_type from "
                f"information_schema.columns WHERE table_name = '{self._table}' "
                f"AND table_schema = '{schema}' AND "
                "data_type not in ('GEOMETRY', 'WKB_BLOB')"
            ).fetchall()
        else:
            rows = self._con.sql(f"DESCRIBE {self._sql}").fetchall()
            field_info = [
                (r[0], r[1])
                for r in rows
                if r[1] not in ("GEOMETRY", "WKB_BLOB") and r[0] != "rowid"
            ]

        for field_name, field_type in field_info:
            qgs_type = self.mapping_field_type.get(field_type)
            if qgs_type is None:
                # Fall back to string for unknown types rather than dropping the column.
                qgs_type = self.mapping_field_type.get("VARCHAR")
            self._fields.append(QgsField(field_name, qgs_type))
        return self._fields

    # -- QGIS provider API -----------------------------------------------------

    def dataSourceUri(self, expandAuthConfig: bool = False) -> str:
        return self._uri

    def crs(self) -> QgsCoordinateReferenceSystem:
        return self._crs

    def featureSource(self):
        return gizmosql_feature_source.GizmoSqlFeatureSource(self)

    def get_table(self) -> str:
        return "" if self._sql else (self._table or "")

    def is_view(self) -> bool:
        if self._sql:
            return False
        query = (
            "SELECT concat(table_schema,'.',table_name) as table_name "
            "FROM information_schema.tables WHERE table_type = 'VIEW'"
        )
        view_list = [row[0] for row in self._con.sql(query).fetchall()]
        return f"{self._schema or 'main'}.{self._table}" in view_list

    def uniqueValues(self, fieldIndex: int, limit: int = -1) -> set:
        column_name = self.fields().field(fieldIndex).name()
        query = (
            f"select distinct {column_name} from {self._from_clause} "
            f"order by {column_name}"
        )
        if limit >= 0:
            query += f" limit {limit}"
        return {row[0] for row in self._con.sql(query).fetchall()}

    def getFeatures(self, request=QgsFeatureRequest()) -> QgsFeature:
        return QgsFeatureIterator(
            gizmosql_feature_iterator.GizmoSqlFeatureIterator(
                gizmosql_feature_source.GizmoSqlFeatureSource(self), request
            )
        )

    def subsetString(self) -> Optional[str]:
        return self.filter_where_clause

    def setSubsetString(
        self, subsetstring: str, updateFeatureCount: bool = True
    ) -> bool:
        if subsetstring:
            try:
                with self._con.cursor() as cur:
                    cur.execute(
                        f"select count(*) from {self._from_clause} "
                        f"WHERE {subsetstring} LIMIT 0"
                    )
            except Exception as e:
                PlgLogger.log(
                    self.tr(f"SQL error in filter: {e}"),
                    log_level=Qgis.MessageLevel.Critical,
                    duration=5,
                    push=False,
                )
                return False
            self.filter_where_clause = subsetstring
        else:
            self.filter_where_clause = None

        if updateFeatureCount:
            self._feature_count = None
            self.reloadData()
        return True

    def supportsSubsetString(self) -> bool:
        return True

    def get_field_index_by_type(self, field_type: QMetaType) -> list:
        fields_index = []
        for i in range(self._fields.count()):
            if self._fields[i].type() == field_type:
                fields_index.append(i)
        return fields_index

    # -- Write support ---------------------------------------------------------

    def _qualified_table(self) -> str:
        """Return a double-quoted schema.table identifier."""
        schema = self._schema or "main"
        return f'"{schema}"."{self._table}"'

    @staticmethod
    def _sql_literal(value) -> str:
        """Convert a Python value to a safe SQL literal (no driver-side params)."""
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        # String / date / datetime — escape single quotes by doubling them.
        return "'" + str(value).replace("'", "''") + "'"

    def _pk_column_name(self) -> Optional[str]:
        """Return the primary-key column name, or None if none detected."""
        pk_idx = self.primary_key()
        if pk_idx == -1:
            return None
        return self.fields().field(pk_idx).name()

    def addFeatures(
        self, flist: list, flags=QgsVectorDataProvider.Flags()
    ) -> tuple[bool, list]:
        if not self._is_valid or self._sql:
            return False, []

        tbl = self._qualified_table()
        flds = self.fields()
        added = []

        try:
            with self._con.cursor() as cur:
                for feat in flist:
                    col_parts = []
                    val_parts = []

                    # Non-geometry attributes
                    for i in range(flds.count()):
                        col_parts.append(f'"{flds.field(i).name()}"')
                        val_parts.append(self._sql_literal(feat.attribute(i)))

                    # Geometry
                    if self._column_geom and not feat.geometry().isNull():
                        wkt = feat.geometry().asWkt()
                        col_parts.append(f'"{self._column_geom}"')
                        val_parts.append(f"ST_GeomFromText('{wkt}')")

                    sql = (
                        f"INSERT INTO {tbl} ({', '.join(col_parts)}) "
                        f"VALUES ({', '.join(val_parts)})"
                    )
                    cur.execute(sql)
                    added.append(feat)

            self._feature_count = None
            self._extent = None
            return True, added
        except Exception as exc:
            PlgLogger.log(
                message=f"addFeatures failed: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=True,
            )
            return False, []

    def deleteFeatures(self, ids: list) -> bool:
        if not self._is_valid or self._sql or not ids:
            return False

        pk = self._pk_column_name()
        if pk is None:
            PlgLogger.log(
                message="deleteFeatures: table has no primary key — cannot delete.",
                log_level=Qgis.MessageLevel.Warning,
                push=True,
            )
            return False

        tbl = self._qualified_table()
        id_list = ", ".join(str(i) for i in ids)
        sql = f'DELETE FROM {tbl} WHERE "{pk}" IN ({id_list})'

        try:
            with self._con.cursor() as cur:
                cur.execute(sql)
            self._feature_count = None
            self._extent = None
            return True
        except Exception as exc:
            PlgLogger.log(
                message=f"deleteFeatures failed: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=True,
            )
            return False

    def changeAttributeValues(self, attr_map: dict) -> bool:
        """attr_map: {fid: {field_index: new_value, ...}, ...}"""
        if not self._is_valid or self._sql or not attr_map:
            return False

        pk = self._pk_column_name()
        if pk is None:
            PlgLogger.log(
                message="changeAttributeValues: table has no primary key.",
                log_level=Qgis.MessageLevel.Warning,
                push=True,
            )
            return False

        tbl = self._qualified_table()
        flds = self.fields()

        try:
            with self._con.cursor() as cur:
                for fid, field_map in attr_map.items():
                    set_clauses = [
                        f'"{flds.field(fidx).name()}" = {self._sql_literal(val)}'
                        for fidx, val in field_map.items()
                    ]
                    sql = (
                        f"UPDATE {tbl} SET {', '.join(set_clauses)} "
                        f'WHERE "{pk}" = {fid}'
                    )
                    cur.execute(sql)
            return True
        except Exception as exc:
            PlgLogger.log(
                message=f"changeAttributeValues failed: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=True,
            )
            return False

    def changeGeometryValues(self, geom_map: dict) -> bool:
        """geom_map: {fid: QgsGeometry, ...}"""
        if not self._is_valid or self._sql or not self._column_geom or not geom_map:
            return False

        pk = self._pk_column_name()
        if pk is None:
            PlgLogger.log(
                message="changeGeometryValues: table has no primary key.",
                log_level=Qgis.MessageLevel.Warning,
                push=True,
            )
            return False

        tbl = self._qualified_table()
        geom_col = f'"{self._column_geom}"'

        try:
            with self._con.cursor() as cur:
                for fid, geom in geom_map.items():
                    wkt = geom.asWkt()
                    sql = (
                        f"UPDATE {tbl} SET {geom_col} = ST_GeomFromText('{wkt}') "
                        f'WHERE "{pk}" = {fid}'
                    )
                    cur.execute(sql)
            self._extent = None
            return True
        except Exception as exc:
            PlgLogger.log(
                message=f"changeGeometryValues failed: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=True,
            )
            return False
