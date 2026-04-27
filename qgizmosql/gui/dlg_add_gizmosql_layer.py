"""Dialog for connecting to a GizmoSQL server and adding a spatial layer.

Built programmatically (no .ui file) because the connection fields are
fundamentally different from the upstream QDuckDB file-picker dialog.
"""

from typing import Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsProviderRegistry,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.gui import QgsAuthConfigSelect, QgsProjectionSelectionWidget
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from qgizmosql.__about__ import DIR_PLUGIN_ROOT
from qgizmosql.provider.gizmosql_wrapper import (
    DEFAULT_GIZMOSQL_PORT,
    GizmoSqlConnConfig,
    GizmoSqlTools,
)
from qgizmosql.toolbelt.log_handler import PlgLogger


class LoadGizmoSqlLayerDialog(QDialog):
    """Connect to a GizmoSQL server and add one of its tables as a QGIS layer."""

    # QgsSettings key prefix for saved connections
    _SETTINGS_KEY = "qgizmosql/connections"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add GizmoSQL layer")
        self.setWindowIcon(
            QIcon(str(DIR_PLUGIN_ROOT.joinpath("resources/images/logo_gizmosql.png")))
        )
        self.resize(560, 600)

        self._wrapper: Optional[GizmoSqlTools] = None
        self._build_ui()
        self._wire_up()
        self._populate_saved_connections()

    # -- UI construction -------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # -- Saved connections group ------------------------------------------
        saved_box = QGroupBox("Saved connections")
        saved_row = QHBoxLayout(saved_box)

        self._saved_combo = QComboBox()
        self._saved_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._saved_combo.setMinimumWidth(200)
        saved_row.addWidget(self._saved_combo, 1)

        self._load_conn_btn = QPushButton("Load")
        self._save_conn_btn = QPushButton("Save")
        self._delete_conn_btn = QPushButton("Delete")
        saved_row.addWidget(self._load_conn_btn)
        saved_row.addWidget(self._save_conn_btn)
        saved_row.addWidget(self._delete_conn_btn)

        root.addWidget(saved_box)

        # -- Connection group --------------------------------------------------
        conn_box = QGroupBox("Connection")
        conn_form = QFormLayout(conn_box)

        self._host_edit = QLineEdit("localhost")
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(DEFAULT_GIZMOSQL_PORT)

        self._use_tls_cb = QCheckBox("Use TLS (grpc+tls)")
        self._use_tls_cb.setChecked(True)
        self._tls_skip_cb = QCheckBox("Skip TLS cert verification (self-signed)")
        self._tls_skip_cb.setChecked(False)

        self._auth_type_combo = QComboBox()
        self._auth_type_combo.addItem("Password", userData="password")
        self._auth_type_combo.addItem(
            "OAuth / SSO (Enterprise Edition)", userData="external"
        )

        self._authcfg_select = QgsAuthConfigSelect(self, "basic")
        self._authcfg_hint = QLabel(
            "Pick or create a stored credential. The password will be saved "
            "encrypted by the QGIS Auth Manager, not in the project file."
        )
        self._authcfg_hint.setWordWrap(True)
        self._authcfg_hint.setStyleSheet("color: #555; font-size: 11px;")

        conn_form.addRow("Host:", self._host_edit)
        conn_form.addRow("Port:", self._port_spin)
        conn_form.addRow("", self._use_tls_cb)
        conn_form.addRow("", self._tls_skip_cb)
        conn_form.addRow("Auth type:", self._auth_type_combo)
        conn_form.addRow("Credentials:", self._authcfg_select)
        conn_form.addRow("", self._authcfg_hint)

        self._connect_btn = QPushButton("Connect && list tables")
        conn_form.addRow("", self._connect_btn)

        root.addWidget(conn_box)

        # -- Layer group -------------------------------------------------------
        layer_box = QGroupBox("Layer")
        layer_v = QVBoxLayout(layer_box)

        # Table vs SQL mode selector
        mode_row = QHBoxLayout()
        self._table_radio = QRadioButton("Table")
        self._table_radio.setChecked(True)
        self._sql_radio = QRadioButton("Custom SQL")
        mode_row.addWidget(self._table_radio)
        mode_row.addWidget(self._sql_radio)
        mode_row.addStretch(1)
        layer_v.addLayout(mode_row)

        self._table_combo = QComboBox()
        self._table_combo.setEnabled(False)
        layer_v.addWidget(QLabel("Schema.table:"))
        layer_v.addWidget(self._table_combo)

        self._sql_edit = QPlainTextEdit()
        self._sql_edit.setPlaceholderText(
            "SELECT id, name, geom FROM schema.my_spatial_table WHERE ..."
        )
        self._sql_edit.setEnabled(False)
        layer_v.addWidget(QLabel("SQL:"))
        layer_v.addWidget(self._sql_edit)

        self._crs_select = QgsProjectionSelectionWidget()
        self._crs_select.setCrs(QgsCoordinateReferenceSystem.fromEpsgId(4326))
        layer_v.addWidget(QLabel("Geometry CRS:"))
        layer_v.addWidget(self._crs_select)

        root.addWidget(layer_box)

        # -- Status + buttons --------------------------------------------------
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #a00;")
        root.addWidget(self._status_label)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._add_layer_btn = self._buttons.addButton(
            "Add Layer", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._add_layer_btn.setIcon(QgsApplication.getThemeIcon("mActionAddLayer.svg"))
        self._add_layer_btn.setEnabled(False)
        root.addWidget(self._buttons)

    def _wire_up(self) -> None:
        self._auth_type_combo.currentIndexChanged.connect(self._on_auth_type_changed)
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._table_radio.toggled.connect(self._on_mode_changed)
        self._table_combo.currentTextChanged.connect(self._refresh_add_enabled)
        self._sql_edit.textChanged.connect(self._refresh_add_enabled)
        self._add_layer_btn.clicked.connect(self._on_add_layer_clicked)
        self._buttons.rejected.connect(self.reject)
        self._load_conn_btn.clicked.connect(self._on_load_connection)
        self._save_conn_btn.clicked.connect(self._on_save_connection)
        self._delete_conn_btn.clicked.connect(self._on_delete_connection)

    # -- Saved connection helpers ----------------------------------------------

    def _qgs_settings(self) -> QgsSettings:
        return QgsSettings()

    def _saved_connection_names(self) -> list:
        s = self._qgs_settings()
        s.beginGroup(self._SETTINGS_KEY)
        names = s.childGroups()
        s.endGroup()
        return sorted(names)

    def _populate_saved_connections(self) -> None:
        self._saved_combo.clear()
        self._saved_combo.addItem("")
        for name in self._saved_connection_names():
            self._saved_combo.addItem(name)

    def _on_load_connection(self) -> None:
        name = self._saved_combo.currentText().strip()
        if not name:
            self._status_label.setStyleSheet("color: #a00;")
            self._status_label.setText("Select a saved connection to load.")
            return
        s = self._qgs_settings()
        s.beginGroup(f"{self._SETTINGS_KEY}/{name}")
        self._host_edit.setText(s.value("host", "localhost"))
        self._port_spin.setValue(int(s.value("port", 31337)))
        self._use_tls_cb.setChecked(s.value("use_tls", True, type=bool))
        self._tls_skip_cb.setChecked(s.value("tls_skip_verify", False, type=bool))
        auth_type = s.value("auth_type", "password")
        idx = self._auth_type_combo.findData(auth_type)
        if idx >= 0:
            self._auth_type_combo.setCurrentIndex(idx)
        authcfg = s.value("authcfg", "")
        if authcfg:
            self._authcfg_select.setConfigId(authcfg)
        s.endGroup()
        self._status_label.setStyleSheet("color: #060;")
        self._status_label.setText(f"Loaded connection '{name}'.")

    def _on_save_connection(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "Save connection",
            "Connection name:",
            text=self._saved_combo.currentText().strip() or self._host_edit.text().strip(),
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        conn = self._current_conn_config()
        s = self._qgs_settings()
        s.beginGroup(f"{self._SETTINGS_KEY}/{name}")
        s.setValue("host", conn.host)
        s.setValue("port", conn.port)
        s.setValue("use_tls", conn.use_tls)
        s.setValue("tls_skip_verify", conn.tls_skip_verify)
        s.setValue("auth_type", conn.auth_type)
        s.setValue("authcfg", conn.authcfg or "")
        s.endGroup()
        self._populate_saved_connections()
        idx = self._saved_combo.findText(name)
        if idx >= 0:
            self._saved_combo.setCurrentIndex(idx)
        self._status_label.setStyleSheet("color: #060;")
        self._status_label.setText(f"Connection '{name}' saved.")

    def _on_delete_connection(self) -> None:
        name = self._saved_combo.currentText().strip()
        if not name:
            return
        reply = QMessageBox.question(
            self,
            "Delete connection",
            f"Delete saved connection '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        s = self._qgs_settings()
        s.beginGroup(self._SETTINGS_KEY)
        s.remove(name)
        s.endGroup()
        self._populate_saved_connections()
        self._status_label.setStyleSheet("color: #060;")
        self._status_label.setText(f"Connection '{name}' deleted.")

    # -- signal handlers -------------------------------------------------------

    def _on_auth_type_changed(self) -> None:
        is_oauth = self._auth_type_combo.currentData() == "external"
        self._authcfg_select.setEnabled(not is_oauth)
        self._authcfg_hint.setText(
            "OAuth/SSO opens a browser window at connect time — no stored credentials needed."
            if is_oauth
            else "Pick or create a stored credential. The password is saved encrypted "
            "by the QGIS Auth Manager, not in the project file."
        )

    def _on_mode_changed(self) -> None:
        table_mode = self._table_radio.isChecked()
        self._table_combo.setEnabled(table_mode)
        self._sql_edit.setEnabled(not table_mode)
        self._refresh_add_enabled()

    def _current_conn_config(self) -> GizmoSqlConnConfig:
        auth_type = self._auth_type_combo.currentData() or "password"
        authcfg = (
            self._authcfg_select.configId() if auth_type == "password" else None
        )
        return GizmoSqlConnConfig(
            host=self._host_edit.text().strip(),
            port=self._port_spin.value(),
            use_tls=self._use_tls_cb.isChecked(),
            tls_skip_verify=self._tls_skip_cb.isChecked(),
            auth_type=auth_type,
            authcfg=authcfg,
        )

    def _on_connect_clicked(self) -> None:
        self._status_label.setText("")
        conn_config = self._current_conn_config()
        if not conn_config.host:
            self._status_label.setText("Host is required.")
            return
        if conn_config.auth_type == "password" and not conn_config.authcfg:
            self._status_label.setText(
                "Pick a QGIS auth config (or create one) with your GizmoSQL username and password."
            )
            return

        try:
            self._wrapper = GizmoSqlTools(conn_config=conn_config)
            rows = self._wrapper.run_sql("list_tables")
        except Exception as exc:
            self._status_label.setText(f"Connection failed: {exc}")
            PlgLogger.log(
                message=f"GizmoSQL connection failed: {exc}",
                log_level=Qgis.MessageLevel.Critical,
                push=True,
            )
            return

        self._table_combo.clear()
        self._table_combo.addItems([r[0] for r in rows])
        self._status_label.setStyleSheet("color: #060;")
        self._status_label.setText(
            f"Connected — found {len(rows)} table(s) on {conn_config.display_name()}."
        )
        self._refresh_add_enabled()

    def _refresh_add_enabled(self) -> None:
        if self._table_radio.isChecked():
            self._add_layer_btn.setEnabled(bool(self._table_combo.currentText()))
        else:
            text = self._sql_edit.toPlainText().strip()
            self._add_layer_btn.setEnabled("select" in text.lower())

    def _on_add_layer_clicked(self) -> None:
        conn_config = self._current_conn_config()
        epsg = (self._crs_select.crs().authid() or "").replace("EPSG:", "") or None

        schema: Optional[str] = None
        table: Optional[str] = None
        sql: Optional[str] = None
        layer_name = ""

        if self._table_radio.isChecked():
            full = self._table_combo.currentText()
            if not full:
                return
            if "." in full:
                schema, table = full.split(".", 1)
            else:
                schema, table = "main", full
            layer_name = full
        else:
            sql = self._sql_edit.toPlainText().strip()
            if not sql:
                return
            layer_name = "gizmosql_query"

        metadata = QgsProviderRegistry.instance().providerMetadata("gizmosql")
        parts = {
            "host": conn_config.host,
            "port": str(conn_config.port),
            "use_tls": "1" if conn_config.use_tls else "0",
            "tls_skip_verify": "1" if conn_config.tls_skip_verify else "0",
            "auth_type": conn_config.auth_type,
        }
        if conn_config.authcfg:
            parts["authcfg"] = conn_config.authcfg
        if table:
            parts["table"] = table
        if schema:
            parts["schema"] = schema
        if sql:
            parts["sql"] = sql
        if epsg:
            parts["epsg"] = epsg

        uri = metadata.encodeUri(parts)
        layer = QgsVectorLayer(uri, layer_name, "gizmosql")
        if not layer.isValid():
            self._status_label.setStyleSheet("color: #a00;")
            self._status_label.setText(
                "Layer could not be loaded. Check the QGIS Log Messages panel."
            )
            return
        QgsProject.instance().addMapLayer(layer)
        self.accept()
