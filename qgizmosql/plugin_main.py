#! python3  # noqa: E265

"""
Main plugin module.
"""

# standard
from __future__ import annotations

import typing
from functools import partial
from pathlib import Path
from typing import Optional

# PyQGIS
from qgis.core import Qgis, QgsApplication, QgsProject, QgsProviderRegistry, QgsSettings
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import QCoreApplication, QLocale, QTranslator, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction

if typing.TYPE_CHECKING:
    from qgis.server import QgsServerInterface

# project
from qgizmosql.__about__ import (
    DIR_PLUGIN_ROOT,
    __icon_path__,
    __title__,
    __uri_homepage__,
)

# plugin
from qgizmosql.gui.dlg_settings import PlgOptionsFactory
from qgizmosql.toolbelt.log_handler import PlgLogger

# conditional imports
try:
    from qgizmosql.gui.dlg_add_gizmosql_layer import LoadGizmoSqlLayerDialog
    from qgizmosql.gui.dlg_sql_editor import SqlEditorDialog
    from qgizmosql.provider.gizmosql_provider_metadata import GizmoSqlProviderMetadata

    EXTERNAL_DEPENDENCIES_AVAILABLE = True
except ImportError:
    EXTERNAL_DEPENDENCIES_AVAILABLE = False

# ############################################################################
# ########## Classes ###############
# ##################################


class QgizmosqlBasePlugin:
    def __init__(self):
        """Constructor.

        This contains common method for the plugin classes.
        """
        self.log = PlgLogger().log

    def tr(self, message: str) -> str:
        """Get the translation for a string using Qt translation API.

        :param message: string to be translated.
        :type message: str

        :returns: Translated version of message.
        :rtype: str
        """
        return QCoreApplication.translate(self.__class__.__name__, message)

    @staticmethod
    def register_gizmosql_provider() -> None:
        """Register the qgizmosql provider.

        Only needs to be called once per QGIS session.
        """
        registry = QgsProviderRegistry.instance()
        registry.registerProvider(GizmoSqlProviderMetadata())


class QgizmosqlPlugin(QgizmosqlBasePlugin):
    def __init__(self, iface: QgisInterface):
        """Constructor.

        :param iface: An interface instance that will be passed to this class which \
        provides the hook by which you can manipulate the QGIS application at run time.
        :type iface: QgsInterface
        """
        super().__init__()

        self.iface = iface

        # translation
        # initialize the locale
        self.locale: str = QgsSettings().value("locale/userLocale", QLocale().name())[
            0:2
        ]
        locale_path: Path = (
            DIR_PLUGIN_ROOT / f"resources/i18n/{__title__.lower()}_{self.locale}.qm"
        )
        self.log(
            message=f"Translation: {self.locale}, {locale_path}",
            log_level=Qgis.MessageLevel.NoLevel,
        )
        if locale_path.exists():
            self.translator = QTranslator()
            self.translator.load(str(locale_path.resolve()))
            QCoreApplication.installTranslator(self.translator)

        # dialogs placeholders
        self._dlg_add_layer: Optional[LoadGizmoSqlLayerDialog] = None
        self._dlg_sql_editor: Optional[SqlEditorDialog] = None

    def initGui(self):
        """Set up plugin UI elements."""

        # settings page within the QGIS preferences menu
        self.options_factory = PlgOptionsFactory()
        self.iface.registerOptionsWidgetFactory(self.options_factory)

        # -- Actions
        self.action_help = QAction(
            QgsApplication.getThemeIcon("mActionHelpContents.svg"),
            self.tr("Help"),
            self.iface.mainWindow(),
        )
        self.action_help.triggered.connect(
            partial(QDesktopServices.openUrl, QUrl(__uri_homepage__))
        )

        self.action_settings = QAction(
            QgsApplication.getThemeIcon("console/iconSettingsConsole.svg"),
            self.tr("Settings"),
            self.iface.mainWindow(),
        )
        self.action_settings.triggered.connect(
            lambda: self.iface.showOptionsDialog(
                currentPage="mOptionsPage{}".format(__title__)
            )
        )

        self.action_main = QAction(
            QIcon(str(__icon_path__.resolve())),
            self.tr("Add GizmoSQL layer"),
            self.iface.mainWindow(),
        )
        self.iface.addToolBarIcon(self.action_main)
        self.action_main.triggered.connect(self.display_gizmosql_dialog)

        self.action_sql_editor = QAction(
            QgsApplication.getThemeIcon("console/iconRunConsole.svg"),
            self.tr("GizmoSQL SQL Editor"),
            self.iface.mainWindow(),
        )
        self.iface.addToolBarIcon(self.action_sql_editor)
        self.action_sql_editor.triggered.connect(self.display_sql_editor)

        # -- Menu
        self.iface.addPluginToMenu(__title__, self.action_main)
        self.iface.addPluginToMenu(__title__, self.action_sql_editor)
        self.iface.addPluginToMenu(__title__, self.action_settings)
        self.iface.addPluginToMenu(__title__, self.action_help)

        # -- Help menu

        # documentation
        self.iface.pluginHelpMenu().addSeparator()
        self.action_help_plugin_menu_documentation = QAction(
            QIcon(str(__icon_path__)),
            f"{__title__} - Documentation",
            self.iface.mainWindow(),
        )
        self.action_help_plugin_menu_documentation.triggered.connect(
            partial(QDesktopServices.openUrl, QUrl(__uri_homepage__))
        )

        self.iface.pluginHelpMenu().addAction(
            self.action_help_plugin_menu_documentation
        )

        if not self.check_dependencies():
            return

        # below come everything which depends on external dependencies
        self._dlg_add_layer = LoadGizmoSqlLayerDialog(self.iface.mainWindow())
        self._dlg_sql_editor = SqlEditorDialog(self.iface.mainWindow())

        # register custom provider
        self.register_gizmosql_provider()
        QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_removal)

    def unload(self):
        """Cleans up when plugin is disabled/uninstalled."""
        # -- Clean up menu
        self.iface.removePluginMenu(__title__, self.action_main)
        self.iface.removePluginMenu(__title__, self.action_sql_editor)
        self.iface.removePluginMenu(__title__, self.action_help)
        self.iface.removePluginMenu(__title__, self.action_settings)

        # -- Clean up toolbar
        self.iface.removeToolBarIcon(self.action_main)
        self.iface.removeToolBarIcon(self.action_sql_editor)

        # -- Clean up preferences panel in QGIS settings
        self.iface.unregisterOptionsWidgetFactory(self.options_factory)

        # remove from QGIS help/extensions menu
        if self.action_help_plugin_menu_documentation:
            self.iface.pluginHelpMenu().removeAction(
                self.action_help_plugin_menu_documentation
            )

        # remove actions
        del self.action_settings
        del self.action_help

    def _on_layers_removal(self, layer_ids: list[str]) -> None:
        """Disconnect the ADBC connection when a qgizmosql-backed layer is removed."""
        for layer_id in layer_ids:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is None:
                continue
            provider = layer.dataProvider()
            if provider and provider.name() == "gizmosql":
                provider.disconnect_database()

    def display_gizmosql_dialog(self) -> None:
        """Display the GizmoSQL add-layer dialog."""
        if self._dlg_add_layer is None:
            self._dlg_add_layer = LoadGizmoSqlLayerDialog()
        self._dlg_add_layer.show()

    def display_sql_editor(self) -> None:
        """Display the GizmoSQL SQL editor dialog."""
        if self._dlg_sql_editor is None:
            self._dlg_sql_editor = SqlEditorDialog()
        self._dlg_sql_editor.show()
        self._dlg_sql_editor.raise_()

    def check_dependencies(self) -> bool:
        """Check if all dependencies are satisfied. If not, warn the user and disable plugin.

        :return: dependencies status
        :rtype: bool
        """
        # if import failed
        if not EXTERNAL_DEPENDENCIES_AVAILABLE:
            self.log(
                message=self.tr("Error importing dependencies. Plugin disabled."),
                log_level=Qgis.MessageLevel.Critical,
                push=True,
                duration=60,
                button=True,
                button_text=self.tr("How to fix it..."),
                button_connect=partial(
                    QDesktopServices.openUrl,
                    QUrl(f"{__uri_homepage__}/usage/installation.html"),
                ),
            )
            # disable plugin widgets
            self.action_main.setEnabled(False)

            # add tooltip over menu
            msg_disable = self.tr(
                "Plugin disabled. Please install all dependencies and then restart QGIS."
                " Refer to the documentation for more information."
            )
            self.action_main.setToolTip(msg_disable)
            return False
        else:
            self.log(
                message=self.tr("Dependencies satisfied"),
                log_level=Qgis.MessageLevel.Success,
            )
            return True


class QgizmosqlServerPlugin(QgizmosqlBasePlugin):
    def __init__(self, serverIface: QgsServerInterface):
        """Constructor.

        :param serverIface: An interface instance that will be passed to this \
        class which provides the hook by which you can manipulate QGIS SERVER \
        at run time.
        :type serverIface: QgsServerInterface
        """
        super().__init__()

        if not EXTERNAL_DEPENDENCIES_AVAILABLE:
            self.log(
                message=self.tr("Error importing dependencies. Plugin disabled."),
                log_level=Qgis.MessageLevel.Critical,
            )
            return

        # QGIS Server only needs to load the provider
        self.register_gizmosql_provider()
        self.log(
            message=self.tr("Dependencies satisfied"),
            log_level=Qgis.MessageLevel.NoLevel,
        )
        return
