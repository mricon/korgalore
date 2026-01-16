"""GNOME Taskbar Application for Korgalore."""

import logging
import math
import signal
import subprocess
import threading
import time
from typing import Optional, Any

import click

from korgalore import AuthenticationError
from korgalore.cli import perform_pull, perform_yank, get_xdg_config_dir, validate_config_file, load_config
from korgalore import RemoteError
from korgalore.bozofilter import ensure_bozofilter_exists, load_bozofilter
from korgalore.gmail_target import GmailTarget
from korgalore.imap_target import ImapTarget

# Optional GTK/AppIndicator3 support - checked at runtime
HAS_GTK = False
try:
    import gi  # type: ignore
    gi.require_version('Gtk', '3.0')
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import Gtk, GLib, AppIndicator3  # type: ignore
    HAS_GTK = True
except (ValueError, ImportError):
    Gtk = None
    GLib = None
    AppIndicator3 = None

logger = logging.getLogger('korgalore.gui')

class KorgaloreApp:
    """Korgalore Taskbar Application."""

    def __init__(self, ctx: click.Context):
        if not HAS_GTK:
            raise RuntimeError(
                "GUI dependencies not available. Install python3-gi and appindicator3 via system packages."
            )
        self.ctx = ctx
        self.ind = AppIndicator3.Indicator.new(
            "korgalore-indicator",
            "mail-read-symbolic",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        self.ind.set_title("Korgalore")
        self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        # Load config
        config = ctx.obj.get('config', {})
        gui_config = config.get('gui', {})
        self.sync_interval = gui_config.get('sync_interval', 300)
        logger.info('Auto-sync interval set to %d seconds', self.sync_interval)

        # State
        self.is_syncing = False
        self.last_sync_time = 0.0
        self.next_sync_time = 0.0
        self.error_state = False
        self.auth_needed_target: Optional[str] = None  # Target ID needing re-auth

        self.ind.set_menu(self.build_menu())

        self.sync_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

    def build_menu(self) -> Any:
        menu = Gtk.Menu()

        # Header
        item_header = Gtk.MenuItem(label="Korgalore")
        item_header.set_sensitive(False)
        menu.append(item_header)

        menu.append(Gtk.SeparatorMenuItem())

        # Sync Now
        self.item_sync = Gtk.MenuItem(label="Sync Now")
        self.item_sync.connect("activate", self.on_sync_now)
        menu.append(self.item_sync)

        # Yank
        item_yank = Gtk.MenuItem(label="Yank...")
        item_yank.connect("activate", self.on_yank)
        menu.append(item_yank)

        # Authenticate (hidden by default, shown when auth is needed)
        self.item_auth = Gtk.MenuItem(label="Authenticate...")
        self.item_auth.connect("activate", self.on_authenticate)
        self.item_auth.set_no_show_all(True)  # Don't show in show_all()
        menu.append(self.item_auth)

        # Separator
        menu.append(Gtk.SeparatorMenuItem())

        # Status Label (Disabled item acting as status)
        self.item_status = Gtk.MenuItem(label="Idle")
        self.item_status.set_sensitive(False)
        menu.append(self.item_status)

        # Next Sync Info
        self.item_next_sync = Gtk.MenuItem(label="Next sync: --:--")
        self.item_next_sync.set_sensitive(False)
        menu.append(self.item_next_sync)

        # Separator
        menu.append(Gtk.SeparatorMenuItem())

        # Edit Config
        item_edit_config = Gtk.MenuItem(label="Edit Config...")
        item_edit_config.connect("activate", self.on_edit_config)
        menu.append(item_edit_config)

        # Edit Bozofilter
        item_edit_bozofilter = Gtk.MenuItem(label="Edit Bozofilter...")
        item_edit_bozofilter.connect("activate", self.on_edit_bozofilter)
        menu.append(item_edit_bozofilter)

        # Quit
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self.quit)
        menu.append(item_quit)

        menu.show_all()
        return menu

    def run(self) -> None:
        """Start the application."""
        # Start background sync thread
        self.sync_thread = threading.Thread(target=self.background_worker, daemon=True)
        self.sync_thread.start()

        # Start timer update loop (every 1 second)
        GLib.timeout_add_seconds(1, self.update_timers)

        # Handle Ctrl+C
        signal.signal(signal.SIGINT, lambda *args: self.quit())

        # Start GTK loop
        Gtk.main()

    def quit(self, source: Any = None) -> None:
        logger.info("Quitting Korgalore GUI...")
        self.stop_event.set()
        Gtk.main_quit()

    def update_status(self, text: str, icon_name: Optional[str] = None) -> None:
        """Update UI status (thread-safe)."""
        def _update() -> bool:
            self.item_status.set_label(text)
            if icon_name:
                self.ind.set_icon(icon_name)
            return False
        GLib.idle_add(_update)

    def update_timers(self) -> bool:
        """Update last/next sync timers in menu."""
        now = time.time()

        # Next Sync
        if self.is_syncing:
             self.item_next_sync.set_label("Next sync: In progress...")
        elif self.next_sync_time > now:
            diff = int(self.next_sync_time - now)
            if diff > 60:
                mins = round(diff / 60)
                text = f"Next sync: ~{mins} min"
            elif diff >= 10:
                secs = math.ceil(diff / 10) * 10
                text = f"Next sync: ~{secs} sec"
            else:
                text = f"Next sync: in {diff}s"
            self.item_next_sync.set_label(text)
        else:
            # Should be syncing soon or now
            self.item_next_sync.set_label("Next sync: Soon...")

        return True  # Keep calling this

    def on_sync_now(self, source: Any) -> None:
        if self.is_syncing:
            return
        # Run sync in a separate thread to not block UI
        threading.Thread(target=self.run_sync, daemon=True).start()

    def on_yank(self, source: Any) -> None:
        """Show the yank dialog."""
        # Must run dialog on main thread
        GLib.idle_add(self._show_yank_dialog)

    def _show_yank_dialog(self) -> bool:
        """Display the yank dialog (called from GLib.idle_add)."""
        config = self.ctx.obj.get('config', {})
        targets = config.get('targets', {})
        target_names = list(targets.keys())

        if not target_names:
            dialog = Gtk.MessageDialog(
                transient_for=None,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="No targets configured"
            )
            dialog.format_secondary_text("Please configure at least one target in your configuration file.")
            dialog.run()
            dialog.destroy()
            return False

        # Create dialog
        dialog = Gtk.Dialog(
            title="Yank Message",
            transient_for=None,
            flags=0
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        dialog.set_default_size(450, -1)

        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_start(15)
        content_area.set_margin_end(15)
        content_area.set_margin_top(15)
        content_area.set_margin_bottom(15)

        # Message-ID or URL entry
        label_msgid = Gtk.Label(label="Message-ID or lore.kernel.org URL:")
        label_msgid.set_halign(Gtk.Align.START)
        content_area.pack_start(label_msgid, False, False, 0)

        entry_msgid = Gtk.Entry()
        entry_msgid.set_placeholder_text("e.g., <msgid@example.com> or https://lore.kernel.org/...")
        content_area.pack_start(entry_msgid, False, False, 0)

        # Target dropdown (only show if multiple targets)
        combo_target: Optional[Gtk.ComboBoxText] = None
        if len(target_names) > 1:
            label_target = Gtk.Label(label="Target:")
            label_target.set_halign(Gtk.Align.START)
            content_area.pack_start(label_target, False, False, 5)

            combo_target = Gtk.ComboBoxText()
            for name in target_names:
                combo_target.append_text(name)
            combo_target.set_active(0)
            content_area.pack_start(combo_target, False, False, 0)

        # Thread checkbox
        check_thread = Gtk.CheckButton(label="Yank entire thread")
        content_area.pack_start(check_thread, False, False, 10)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            msgid_or_url = entry_msgid.get_text().strip()
            if combo_target is not None:
                target_name = combo_target.get_active_text()
            else:
                target_name = target_names[0]
            fetch_thread = check_thread.get_active()

            dialog.destroy()

            if msgid_or_url and target_name:
                # Run yank in background thread
                threading.Thread(
                    target=self._run_yank,
                    args=(target_name, msgid_or_url, fetch_thread),
                    daemon=True
                ).start()
        else:
            dialog.destroy()

        return False

    def _run_yank(self, target_name: str, msgid_or_url: str, fetch_thread: bool) -> None:
        """Execute the yank operation in background."""
        self.update_status("Yanking...", "system-run-symbolic")

        try:
            uploaded, failed = perform_yank(
                self.ctx, target_name, msgid_or_url, thread=fetch_thread
            )

            if failed > 0:
                self.update_status(f"Yanked {uploaded}, {failed} failed", "dialog-warning-symbolic")
                logger.warning("Yank completed: %d uploaded, %d failed", uploaded, failed)
            else:
                self.update_status(f"Yanked {uploaded} message(s)", "mail-unread-symbolic")
                logger.info("Yank completed: %d message(s) uploaded", uploaded)

        except RemoteError as e:
            logger.error("Yank failed: %s", str(e))
            self.update_status("Yank failed - see logs", "dialog-error-symbolic")
        except Exception as e:
            logger.error("Yank failed: %s", str(e))
            self.update_status("Yank failed - see logs", "dialog-error-symbolic")

    def on_edit_config(self, source: Any) -> None:
        """Open the configuration file in the user's preferred editor."""
        # Run in background thread to not block UI while waiting for editor
        threading.Thread(target=self._run_edit_config, daemon=True).start()

    def _run_edit_config(self) -> None:
        """Execute config editing and validate after editor closes."""
        cfgpath = get_xdg_config_dir() / 'korgalore.toml'
        logger.info("Opening configuration file: %s", cfgpath)
        try:
            proc = subprocess.Popen(['xdg-open', str(cfgpath)])
            proc.wait()

            # Validate after editor closes
            is_valid, error_msg = validate_config_file(cfgpath)
            if is_valid:
                logger.info("Configuration file is valid, reloading...")
                # Reload config and clear cached instances
                self.ctx.obj['config'] = load_config(cfgpath)
                self.ctx.obj['targets'] = dict()
                self.ctx.obj['feeds'] = dict()
                self.ctx.obj['deliveries'] = dict()
                # Update sync interval if changed
                gui_config = self.ctx.obj['config'].get('gui', {})
                self.sync_interval = gui_config.get('sync_interval', 300)
                logger.info("Configuration reloaded successfully.")
            else:
                logger.error("Configuration file has errors: %s", error_msg)
                self.update_status("Config error - see logs", "dialog-warning-symbolic")
        except Exception as e:
            logger.error("Failed to open config file: %s", str(e))

    def on_edit_bozofilter(self, source: Any) -> None:
        """Open the bozofilter file in the user's preferred editor."""
        threading.Thread(target=self._run_edit_bozofilter, daemon=True).start()

    def _run_edit_bozofilter(self) -> None:
        """Execute bozofilter editing and reload after editor closes."""
        config_dir = get_xdg_config_dir()
        bozofilter_path = ensure_bozofilter_exists(config_dir)

        logger.info("Opening bozofilter file: %s", bozofilter_path)
        try:
            proc = subprocess.Popen(['xdg-open', str(bozofilter_path)])
            proc.wait()
            # Reload bozofilter after editor closes
            self.ctx.obj['bozofilter'] = load_bozofilter(config_dir)
            logger.info("Bozofilter reloaded successfully.")
        except Exception as e:
            logger.error("Failed to edit bozofilter: %s", str(e))

    def background_worker(self) -> None:
        """Periodically run sync."""
        logger.info("Background worker started")
        # Initial sync after a short delay
        time.sleep(2)
        if not self.stop_event.is_set():
            self.run_sync()

        while not self.stop_event.is_set():
            # Sleep in 1-second chunks, checking if it's time to sync
            # This handles both the regular interval and manual sync resets
            time.sleep(1)
            if self.stop_event.is_set():
                return
            if time.time() >= self.next_sync_time and not self.is_syncing:
                self.run_sync()

    def run_sync(self) -> None:
        """Execute the pull logic."""
        if self.is_syncing:
            return

        self.is_syncing = True
        GLib.idle_add(lambda: self.item_sync.set_sensitive(False))
        self.update_status("Syncing...", "system-run-symbolic")
        # Ensure next sync shows as processing
        self.next_sync_time = 0

        try:
            logger.info("Starting sync...")

            # Run the pull command logic
            # We wrap this to catch any exceptions and update UI
            _, unique_msgids = perform_pull(
                self.ctx,
                no_update=False,
                force=False,
                delivery_name=None,
                status_callback=lambda s: self.update_status(s, "system-run-symbolic")
            )

            self.last_sync_time = time.time()
            self.error_state = False

            count = len(unique_msgids)

            if count > 0:
                logger.info("Sync complete: %d new messages", count)
                self.update_status(f"Idle ({count} new)", "mail-unread-symbolic")
            else:
                logger.info("Sync complete: no new messages")
                self.update_status("Idle", "mail-read-symbolic")

        except AuthenticationError as e:
            logger.error("Authentication required for %s: %s", e.target_id, str(e))
            self.error_state = True
            self.auth_needed_target = e.target_id
            self.update_status(f"Auth required: {e.target_id}", "dialog-password-symbolic")
            GLib.idle_add(self._show_auth_button)
        except Exception as e:
            logger.error("Sync failed: %s", str(e))
            self.error_state = True
            self.update_status("Error: See logs", "dialog-error-symbolic")
        finally:
            self.is_syncing = False
            # Reset countdown timer after sync completes
            self.next_sync_time = time.time() + self.sync_interval
            GLib.idle_add(lambda: self.item_sync.set_sensitive(True))

    def _show_auth_button(self) -> bool:
        """Show the authenticate button (called from GLib.idle_add)."""
        self.item_auth.show()
        return False

    def _hide_auth_button(self) -> bool:
        """Hide the authenticate button (called from GLib.idle_add)."""
        self.item_auth.hide()
        return False

    def on_authenticate(self, source: Any) -> None:
        """Handle authenticate menu item click."""
        if not self.auth_needed_target:
            return
        # Run authentication in a separate thread to not block UI
        threading.Thread(target=self._run_authenticate, daemon=True).start()

    def _run_authenticate(self) -> None:
        """Execute the re-authentication flow."""
        target_id = self.auth_needed_target
        if not target_id:
            return

        self.update_status(f"Authenticating {target_id}...", "system-run-symbolic")
        GLib.idle_add(lambda: self.item_auth.set_sensitive(False))

        try:
            # Find the target in our context
            targets = self.ctx.obj.get('targets', {})
            target = targets.get(target_id)

            if target is None:
                logger.error("Target %s not found in context", target_id)
                self.update_status("Error: Target not found", "dialog-error-symbolic")
                return

            # Check if target supports re-authentication
            if isinstance(target, GmailTarget):
                # Run the Gmail re-authentication flow (this opens a browser)
                target.reauthenticate()
            elif isinstance(target, ImapTarget) and target.auth_type == 'oauth2':
                # Run the IMAP OAuth2 re-authentication flow (this opens a browser)
                target.reauthenticate()
            else:
                logger.error("Target %s does not support re-authentication", target_id)
                self.update_status("Error: Auth not supported", "dialog-error-symbolic")
                return

            # Success - clear the auth needed state
            self.auth_needed_target = None
            self.error_state = False
            GLib.idle_add(self._hide_auth_button)
            logger.info("Re-authentication successful for %s", target_id)

            # Automatically start sync after successful authentication
            GLib.idle_add(lambda: self.item_auth.set_sensitive(True))
            self.run_sync()
            return

        except Exception as e:
            logger.error("Re-authentication failed: %s", str(e))
            self.update_status(f"Auth failed: {target_id}", "dialog-error-symbolic")
        finally:
            GLib.idle_add(lambda: self.item_auth.set_sensitive(True))


def start_gui(ctx: click.Context) -> None:
    """Entry point for the GUI."""
    # Ensure logging is configured (Click log might catch this, but just in case)
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO)

    app = KorgaloreApp(ctx)
    app.run()
