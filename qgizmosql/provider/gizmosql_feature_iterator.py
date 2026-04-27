# standard
from __future__ import (
    annotations,  # used to manage type annotation for method that return Self in Python < 3.11
)

from typing import Any, Callable

# PyQGIS
from qgis.core import (
    Qgis,
    QgsAbstractFeatureIterator,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType, QTime

# plugin
from qgizmosql.toolbelt.log_handler import PlgLogger
from qgizmosql.toolbelt.preferences import PlgOptionsManager


class GizmoSqlFeatureIterator(QgsAbstractFeatureIterator):
    def __init__(
        self,
        source,
        request: QgsFeatureRequest,
    ):
        """Constructor"""
        super().__init__(request)
        self._provider = source.get_provider()
        self._settings = PlgOptionsManager.get_plg_settings()
        self.log = PlgLogger().log

        self._request = request if request is not None else QgsFeatureRequest()
        self._transform = QgsCoordinateTransform()

        if (
            self._request.destinationCrs().isValid()
            and self._request.destinationCrs() != source._provider.crs()
        ):
            self._transform = QgsCoordinateTransform(
                source._provider.crs(),
                self._request.destinationCrs(),
                self._request.transformContext(),
            )

        try:
            filter_rect = self.filterRectToSourceCrs(self._transform)
        except QgsCsException:
            self.close()
            return

        if not self._provider.isValid():
            return

        geom_column = self._provider.get_geometry_column()

        # Check if some attributes which contain date or time
        # In that case, they need to be converted to a Qt type
        # to be correctly handled by QGIS.
        attributes_conversion_functions: dict[QMetaType.Type, Callable[[Any], Any]] = {
            QMetaType.Type.QDate: QDate,
            QMetaType.Type.QTime: QTime,
            QMetaType.Type.QDateTime: QDateTime,
        }
        # By default, do not convert
        self._attributes_converters = {}
        for idx in range(len(self._provider.fields())):
            self._attributes_converters[idx] = lambda x: x

        # Check if some fields need to be converted
        # If that's the case, enable the _attributes_need_conversion flag
        # and assign the converter with the attributes index.
        self._attributes_need_conversion = False
        for field_type, converter in attributes_conversion_functions.items():
            for index in self._provider.get_field_index_by_type(field_type):
                self._attributes_need_conversion = True
                self._attributes_converters[index] = converter

        # Create the list of fields that need to be retrieved
        self._request_sub_attributes = (
            self._request.flags() & QgsFeatureRequest.Flag.SubsetOfAttributes
        )
        if self._request_sub_attributes and not self._provider.subsetString():
            idx_required = [idx for idx in self._request.subsetOfAttributes()]

            # The primary key column must be added if it is not present in the field list.
            if (
                self._provider.primary_key() != -1
                and self._provider.primary_key() not in idx_required
            ):
                idx_required.append(self._provider.primary_key())

            list_field_names = [
                self._provider.fields()[idx].name() for idx in idx_required
            ]
        else:
            list_field_names = [field.name() for field in self._provider.fields()]

        if len(list_field_names) > 0:
            fields_name_for_query = '"' + '", "'.join(list_field_names) + '"'
        else:
            fields_name_for_query = ""

        if fields_name_for_query:
            fields_name_for_query += ","
        self.index_geom_column = len(list_field_names)

        # Create fid/fids list
        feature_id_list = None
        if (
            self._request.filterType() == QgsFeatureRequest.FilterType.FilterFid
            or self._request.filterType() == QgsFeatureRequest.FilterType.FilterFids
        ):
            feature_id_list = (
                [self._request.filterFid()]
                if self._request.filterType() == QgsFeatureRequest.FilterType.FilterFid
                else self._request.filterFids()
            )

        where_clause_list = []
        if feature_id_list:
            if self._provider.primary_key() == -1:
                feature_clause = f"index in {tuple(feature_id_list)}"
            else:
                primary_key_name = list_field_names[self._provider.primary_key()]
                feature_clause = f"{primary_key_name} in {tuple(feature_id_list)}"

            where_clause_list.append(feature_clause)

        # Apply the filter expression
        if self._request.filterType() == QgsFeatureRequest.FilterType.FilterExpression:
            # A provider is supposed to implement a QgsSqlExpressionCompiler
            # in order to handle expression. However, this class is not
            # available in the Python bindings.
            # Try to use the expression as is. It should work in most
            # cases for simple expression.
            expression = self._request.filterExpression().expression()
            if expression:
                try:
                    cur = self._provider.con()
                    cur.execute(
                        f"SELECT count(*)"
                        f" FROM {self._provider._from_clause}"
                        f" WHERE {expression}"
                        " LIMIT 0"
                    )
                    cur.close()
                    self._expression = expression
                    where_clause_list.append(expression)
                except Exception:
                    PlgLogger.log(
                        f"GizmoSQL provider does not handle expression: {expression}",
                        log_level=Qgis.MessageLevel.Critical,
                        duration=5,
                        push=False,
                    )
                    self._expression = ""
            else:
                self._expression = ""

        # Apply the subset string filter
        if self._provider.subsetString():
            subset_clause = self._provider.subsetString().replace('"', "")
            where_clause_list.append(subset_clause)

        # Apply the geometry filter
        if not filter_rect.isNull():
            filter_geom_clause = (
                f"st_intersects({geom_column}, "
                f"st_geomfromtext('{filter_rect.asWktPolygon()}'))"
            )
            where_clause_list.append(filter_geom_clause)

        # build the complete where clause
        where_clause = ""
        if where_clause_list:
            where_clause = f"where {where_clause_list[0]}"
            if len(where_clause_list) > 1:
                for clause in where_clause_list[1:]:
                    where_clause += f" and {clause}"

        geom_query = f"st_aswkb({geom_column}), {geom_column}, "
        self._request_no_geometry = (
            self._request.flags() & QgsFeatureRequest.Flag.NoGeometry
        )
        if self._request_no_geometry:
            geom_query = ""

        if self._provider.primary_key() == -1:
            index = "ROW_NUMBER() OVER () as index"
            order_by = "index"
        else:
            index = self._provider._fields[self._provider.primary_key()].name()
            order_by = index

        final_query = (
            "select * from ("
            f"select {fields_name_for_query} "
            f"{geom_query} {index} "
            f"from {self._provider._from_clause}) "
            f"{where_clause} "
            f"order by {order_by}"
        )

        if self._settings.debug_mode:
            self.log(
                message="feature iterator execute query: {}".format(final_query),
                log_level=Qgis.MessageLevel.NoLevel,
                push=False,
            )

        # Execute query and stream results as Arrow record batches.
        # This avoids round-tripping one row at a time over the network;
        # instead the ADBC driver pages data in columnar batches which is
        # significantly faster for large layers.
        cur = self._provider.con()
        cur.execute(final_query)
        self._batch_reader = cur.fetch_record_batch_reader()
        self._current_batch = None      # current pyarrow.RecordBatch
        self._batch_row_index = 0       # row offset within the current batch
        self._index = 0

    def _advance_batch(self) -> bool:
        """Load the next Arrow record batch. Returns False when exhausted."""
        try:
            self._current_batch = self._batch_reader.read_next_batch()
            self._batch_row_index = 0
            return True
        except StopIteration:
            self._current_batch = None
            return False

    def fetchFeature(self, f: QgsFeature) -> bool:
        """fetch next feature, return true on success

        :param f: Next feature
        :type f: QgsFeature
        :return: True if success
        :rtype: bool
        """
        if not self._provider.isValid():
            f.setValid(False)
            return False

        # Advance to the next batch when the current one is exhausted or missing.
        while self._current_batch is None or self._batch_row_index >= self._current_batch.num_rows:
            if not self._advance_batch():
                f.setValid(False)
                return False

        batch = self._current_batch
        row = self._batch_row_index
        self._batch_row_index += 1

        # Helper: extract a Python scalar from a pyarrow column at `row`.
        def _val(col_idx: int):
            return batch.column(col_idx)[row].as_py()

        f.setFields(self._provider.fields())
        f.setValid(True)

        if not self._request_no_geometry:
            wkb = _val(self.index_geom_column)
            if wkb is not None:
                geometry = QgsGeometry()
                geometry.fromWkb(bytes(wkb))
                f.setGeometry(geometry)
                self.geometryToDestinationCrs(f, self._transform)

        f.setId(_val(batch.num_columns - 1))

        # set attributes
        if self._attributes_need_conversion:
            if self._request_sub_attributes:
                for idx, attr_idx in enumerate(self._request.subsetOfAttributes()):
                    f.setAttribute(attr_idx, self._attributes_converters[idx](_val(idx)))
            else:
                for idx in range(self.index_geom_column):
                    f.setAttribute(idx, self._attributes_converters[idx](_val(idx)))
        else:
            if self._request_sub_attributes:
                for idx, attr_idx in enumerate(self._request.subsetOfAttributes()):
                    f.setAttribute(attr_idx, _val(idx))
            else:
                f.setAttributes([_val(i) for i in range(self.index_geom_column)])

        self._index += 1
        return True

    def nextFeatureFilterExpression(self, f: QgsFeature) -> bool:
        if not self._expression:
            return super().nextFeatureFilterExpression(f)
        else:
            return self.fetchFeature(f)

    def __iter__(self) -> "GizmoSqlFeatureIterator":
        """Returns self as an iterator object"""
        self._index = 0
        return self

    def __next__(self) -> QgsFeature:
        """Returns the next value till current is lower than high"""
        f = QgsFeature()
        if not self.nextFeature(f):
            raise StopIteration
        else:
            return f

    def rewind(self) -> bool:
        """reset the iterator to the starting position"""
        if self._index < 0:
            return False
        # Arrow record batch readers are forward-only; we cannot rewind them.
        # Return False to signal QGIS to re-create the iterator if needed.
        return False

    def close(self) -> bool:
        """end of iterating: free the resources / lock"""
        self._index = -1
        if getattr(self, "_batch_reader", None) is not None:
            try:
                self._batch_reader.close()
            except Exception:
                pass
            self._batch_reader = None
        self._current_batch = None
        return True
