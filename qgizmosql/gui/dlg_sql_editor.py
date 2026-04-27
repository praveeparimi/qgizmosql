"""SQL Editor dialog for the qgizmosql QGIS plugin.

Lets the user write and execute arbitrary SQL against a GizmoSQL server,
preview results in a table, and optionally add spatial results as a QGIS
vector layer.

Connections are loaded from QgsSettings (saved via the Add Layer dialog).
"""

from __future__ import annotations

from typing import Optional

from qgis.core import Qgis, QgsProject, QgsProviderRegistry, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qgizmosql.__about__ import DIR_PLUGIN_ROOT
from qgizmosql.provider.gizmosql_wrapper import GizmoSqlConnConfig, GizmoSqlTools
from qgizmosql.toolbelt.log_handler import PlgLogger

_SETTINGS_KEY = "qgizmosql/connections"
_MAX_PREVIEW_ROWS = 500
_DEFAULT_SQL = """\
-- Example: list spatial tables
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
ORDER BY table_schema, table_name;
"""


class SqlEditorDialog(QDialog):
    """Execute SQL against a GizmoSQL server and preview / add results as layers."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("GizmoSQL — SQL Editor")
        self.setWindowIcon(
            QIcon(str(DIR_PLUGIN_ROOT / "resources/images/logo_gizmosql.png"))
        )
        self.setMinimumSize(900, 650)

        self._wrapper: Optional[GizmoSqlTools] = None
        self._last_arrow_table = None  # pyarrow.Table from last query

        self._build_ui()
        self._populate_connections()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # -- Connection bar
        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Connection:"))

        self._combo_conn = QComboBox()
        self._combo_conn.setMinimumWidth(220)
        conn_row.addWidget(self._combo_conn)

        self._btn_connect = QPushButton("Connect")
        self._btn_connect.clicked.connect(self._on_connect)
        conn_row.addWidget(self._btn_connect)

        conn_row.addStretch()
        self._lbl_status = QLabel("")
        conn_row.addWidget(self._lbl_status)
        layout.addLayout(conn_row)

        # -- Splitter: editor top, results bottom
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Editor panel
        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        toolbar_row = QHBoxLayout()
        toolbar_row.addWidget(QLabel("SQL Query:"))
        toolbar_row.addStretch()

        self._btn_execute = QPushButton("▶  Run")
        self._btn_execute.setEnabled(False)
        self._btn_execute.setStyleSheet(
            "QPushButton { background-color: #1565c0; color: white;"
            " font-weight: bold; padding: 4px 18px; }"
            "QPushButton:disabled { background-color: #ccc; color: #888; }"
        )
        self._btn_execute.clicked.connect(self._on_execute)
        toolbar_row.addWidget(self._btn_execute)
        editor_layout.addLayout(toolbar_row)

        self._sql_editor = QTextEdit()
        self._sql_editor.setAcceptRichText(False)
        self._sql_editor.setPlainText(_DEFAULT_SQL)
        mono = QFont("Consolas", 11)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._sql_editor.setFont(mono)
        self._sql_editor.setTabStopDistance(28)
        editor_layout.addWidget(self._sql_editor)
        splitter.addWidget(editor_widget)

        # Results panel
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        results_layout.setContentsMargins(0, 0, 0, 0)

        results_toolbar = QHBoxLayout()
        self._lbl_results = QLabel("Results")
        results_toolbar.addWidget(self._lbl_results)
        results_toolbar.addStretch()

        self._btn_add_layer = QPushButton("Add as Layer")
        self._btn_add_layer.setEnabled(False)
        self._btn_add_layer.clicked.connect(self._on_add_as_layer)
        results_toolbar.addWidget(self._btn_add_layer)
        results_layout.addLayout(results_toolbar)

        self._results_table = QTableWidget()
        self._results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._results_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._results_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        results_layout.addWidget(self._results_table)

        # Layer name row
        layer_name_row = QHBoxLayout()
        layer_name_row.addWidget(QLabel("Layer name:"))
        self._edit_layer_name = QLineEdit("sql_result")
        layer_name_row.addWidget(self._edit_layer_name)
        results_layout.addLayout(layer_name_row)

        splitter.addWidget(results_widget)
        splitter.setSizes([300, 350])
        layout.addWidget(splitter)

        # Close button
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # ------------------------------------------------------------------
    # Connections (from QgsSettings saved by the Add Layer dialog)
    # ------------------------------------------------------------------
    def _saved_connection_names(self) -> list[str]:
        s = QgsSettings()
        s.beginGroup(_SETTINGS_KEY)
        names = s.childGroups()
        s.endGroup()
        return sorted(names)

    def _populate_connections(self) -> None:
        self._combo_conn.clear()
        names = self._saved_connection_names()
        for name in names:
            self._combo_conn.addItem(name)
        if not names:
            self._lbl_status.setText("No saved connections — use 'Add GizmoSQL Layer' to save one.")
            self._lbl_status.setStyleSheet("color: #888;")

    def _load_conn_config(self, name: str) -> Optional[GizmoSqlConnConfig]:
        s = QgsSettings()
        s.beginGroup(f"{_SETTINGS_KEY}/{name}")
        host = s.value("host", "")
        port = int(s.value("port", 31337))
        use_tls = s.value("use_tls", True, type=bool)
        tls_skip_verify = s.value("tls_skip_verify", False, type=bool)
        auth_type = s.value("auth_type", "password")
        authcfg = s.value("authcfg", "")
        s.endGroup()
        if not host:
            return None
        return GizmoSqlConnConfig(
            host=host,
            port=port,
            use_tls=use_tls,
            tls_skip_verify=tls_skip_verify,
            auth_type=auth_type,
            authcfg=authcfg or None,
        )

    def _on_connect(self) -> None:
        name = self._combo_conn.currentText().strip()
        if not name:
            return

        conn_config = self._load_conn_config(name)
        if conn_config is None:
            self._set_status("Could not load connection settings.", error=True)
            return

        self._set_status("Connecting…")
        self._btn_execute.setEnabled(False)

        try:
            if self._wrapper:
                self._wrapper.close()
            self._wrapper = GizmoSqlTools(conn_config=conn_config)
            self._wrapper.connect()
            self._set_status(f"Connected to {conn_config.display_name()}", ok=True)
            self._btn_execute.setEnabled(True)
        except Exception as exc:
            self._set_status(f"Connection failed: {exc}", error=True)
            PlgLogger.log(
                message=f"SQL Editor connection failed: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=False,
            )

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------
    def _on_execute(self) -> None:
        sql = self._sql_editor.toPlainText().strip()
        if not sql or not self._wrapper:
            return

        self._lbl_results.setText("Executing…")
        self._lbl_results.setStyleSheet("")
        self._lbl_results.repaint()

        try:
            arrow_table = self._wrapper.run_sql(sql, results_fetcher="fetch_arrow")
            self._last_arrow_table = arrow_table
            self._display_results(arrow_table)
        except Exception as exc:
            self._lbl_results.setText(f"Error: {exc}")
            self._lbl_results.setStyleSheet("color: red;")
            self._results_table.setRowCount(0)
            self._results_table.setColumnCount(0)
            self._btn_add_layer.setEnabled(False)
            PlgLogger.log(
                message=f"SQL Editor query failed: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=False,
            )

    def _display_results(self, table) -> None:
        num_rows = table.num_rows
        num_cols = table.num_columns
        col_names = table.column_names
        display_rows = min(num_rows, _MAX_PREVIEW_ROWS)

        self._results_table.setRowCount(display_rows)
        self._results_table.setColumnCount(num_cols)
        self._results_table.setHorizontalHeaderLabels(col_names)

        for col_idx in range(num_cols):
            column = table.column(col_idx)
            for row_idx in range(display_rows):
                value = column[row_idx].as_py()
                self._results_table.setItem(
                    row_idx, col_idx, QTableWidgetItem("" if value is None else str(value))
                )

        truncated = (
            f" (showing first {_MAX_PREVIEW_ROWS:,})" if num_rows > _MAX_PREVIEW_ROWS else ""
        )
        self._lbl_results.setText(
            f"Results: {num_rows:,} rows × {num_cols} columns{truncated}"
        )
        self._lbl_results.setStyleSheet("color: #060;")

        geom_cols = {"geometry", "geom", "wkb_geometry", "the_geom", "shape", "wkb"}
        has_geom = any(n.lower() in geom_cols for n in col_names)
        self._btn_add_layer.setEnabled(has_geom)

    # ------------------------------------------------------------------
    # Add as Layer
    # ------------------------------------------------------------------
    def _on_add_as_layer(self) -> None:
        if not self._wrapper:
            return
        sql = self._sql_editor.toPlainText().strip()
        if not sql:
            return

        conn = self._wrapper.conn_config
        metadata = QgsProviderRegistry.instance().providerMetadata("gizmosql")
        parts = {
            "host": conn.host,
            "port": str(conn.port),
            "use_tls": "1" if conn.use_tls else "0",
            "tls_skip_verify": "1" if conn.tls_skip_verify else "0",
            "auth_type": conn.auth_type,
            "sql": sql,
        }
        if conn.authcfg:
            parts["authcfg"] = conn.authcfg

        uri = metadata.encodeUri(parts)
        layer_name = self._edit_layer_name.text().strip() or "sql_result"
        layer = QgsVectorLayer(uri, layer_name, "gizmosql")

        if not layer.isValid():
            self._set_status("Layer could not be loaded — check QGIS log.", error=True)
            return

        QgsProject.instance().addMapLayer(layer)
        self._set_status(f'Layer "{layer_name}" added to map.', ok=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _set_status(self, msg: str, *, ok: bool = False, error: bool = False) -> None:
        self._lbl_status.setText(msg)
        if ok:
            self._lbl_status.setStyleSheet("color: #060;")
        elif error:
            self._lbl_status.setStyleSheet("color: #a00;")
        else:
            self._lbl_status.setStyleSheet("color: #555;")

    def closeEvent(self, event) -> None:
        if self._wrapper:
            self._wrapper.close()
        super().closeEvent(event)
