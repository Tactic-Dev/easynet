#!/usr/bin/env python3
"""EasyNet: a simple, clear network tray icon (GTK3 + NetworkManager via libnm).

Tray icon shows wired / Wi-Fi signal / problem. Click for a small panel: what you're connected to,
internet status (including "sign in to network" for hotel portals), Wi-Fi on/off and networks,
Tailscale, and saved Wi-Fi (auto-join / forget). Docker/virtual interfaces are hidden.
"""
import json, os, shutil, subprocess, sys, threading, time, uuid
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("NM", "1.0")
from gi.repository import Gtk, Gdk, Gio, GLib, NM, Pango
try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
except (ValueError, ImportError):
    XApp = None

VERSION = "1.2.1"
APP_ID = "dev.tacticdev.easynet"
AP_FLAGS = getattr(NM, "80211ApFlags")
AP_SEC = getattr(NM, "80211ApSecurityFlags")
PORTAL_PROBE = "http://neverssl.com/"   # plain-http page; hotel portals hijack it and show their login
DOCK_SCRIPT = "/etc/NetworkManager/dispatcher.d/90-dock-wifi-toggle"
HIDDEN_TYPES = (NM.DeviceType.BRIDGE, NM.DeviceType.LOOPBACK, NM.DeviceType.TUN, NM.DeviceType.VETH,
                NM.DeviceType.WIFI_P2P, NM.DeviceType.GENERIC, NM.DeviceType.DUMMY)

CSS = b"""
window.easynet { background-color: transparent; }
.en-card { background-color: #2c2c2c; color: #ffffff; border: 1px solid rgba(255,255,255,0.08);
           border-radius: 18px; padding: 14px 14px 10px; }
.en-title { font-weight: bold; }
.en-dim { color: rgba(255,255,255,0.55); font-size: 9pt; }
.en-ok { color: #3fb950; } .en-warn { color: #e3b341; } .en-bad { color: #e5534b; }
.en-section { font-weight: bold; font-size: 9pt; color: rgba(255,255,255,0.6); margin-top: 6px; }
.en-row { padding: 6px 10px; border-radius: 10px; }
.en-row:hover { background-color: rgba(255,255,255,0.06); }
.en-row.active { background-color: rgba(75,144,237,0.18); }
.en-row.active .en-name { font-weight: bold; }
.en-status { background-color: rgba(255,255,255,0.05); border-radius: 14px; padding: 10px 12px; }
.en-portal { background-color: #4b90ed; color: #ffffff; border-radius: 10px; padding: 6px 12px; }
.en-link { padding: 6px 10px; border-radius: 10px; }
"""


def ssid_text(ssid_bytes):
    if not ssid_bytes: return ""
    return NM.utils_ssid_to_utf8(ssid_bytes.get_data())

def signal_icon(strength):
    if strength >= 75: return "network-wireless-signal-excellent-symbolic"
    if strength >= 50: return "network-wireless-signal-good-symbolic"
    if strength >= 25: return "network-wireless-signal-ok-symbolic"
    return "network-wireless-signal-weak-symbolic"

def ap_security(ap):
    """'open' | 'psk' | 'sae' | 'enterprise' | 'wep'"""
    wpa, rsn = ap.get_wpa_flags(), ap.get_rsn_flags()
    flags = wpa | rsn
    if flags & AP_SEC.KEY_MGMT_802_1X: return "enterprise"
    if flags & AP_SEC.KEY_MGMT_SAE and not flags & AP_SEC.KEY_MGMT_PSK: return "sae"
    if flags & AP_SEC.KEY_MGMT_PSK or flags & AP_SEC.KEY_MGMT_SAE: return "psk"
    if ap.get_flags() & AP_FLAGS.PRIVACY: return "wep"
    return "open"

def ip4_of(dev):
    cfg = dev.get_ip4_config() if dev else None
    addrs = cfg.get_addresses() if cfg else []
    return addrs[0].get_address() if addrs else ""

def run(*cmd, timeout=6):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


class Net:
    """NetworkManager state through libnm; calls on_change (debounced) whenever anything moves."""
    def __init__(self, on_change):
        self.on_change = on_change
        self.c = NM.Client.new(None)
        self._pending = None
        for sig in ("notify::primary-connection", "notify::connectivity", "notify::wireless-enabled",
                    "notify::wireless-hardware-enabled", "device-added", "device-removed",
                    "active-connection-added", "active-connection-removed", "connection-added", "connection-removed"):
            self.c.connect(sig, lambda *_: self.changed())
        for d in self.c.get_devices(): self._watch_dev(d)
        for ac in self.c.get_active_connections(): ac.connect("notify::state", lambda *_: self.changed())
        self.c.connect("active-connection-added", lambda _c, ac: ac.connect("notify::state", lambda *_: self.changed()))
        self.c.connect("device-added", lambda _c, d: self._watch_dev(d))
        self.tailscale = {}
        threading.Thread(target=self._tailscale_loop, daemon=True).start()

    def _watch_dev(self, d):
        d.connect("state-changed", lambda *_: self.changed())
        if isinstance(d, NM.DeviceWifi):
            d.connect("access-point-added", lambda *_: self.changed())
            d.connect("access-point-removed", lambda *_: self.changed())
            d.connect("notify::active-access-point", lambda *_: self.changed())

    def changed(self):
        if self._pending: GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(150, self._fire)

    def _fire(self):
        self._pending = None; self.on_change(); return False

    # ---- queries ----
    def devices(self):
        return [d for d in self.c.get_devices()
                if d.get_device_type() not in HIDDEN_TYPES and d.get_managed()
                and not d.get_iface().startswith(("docker", "br-", "veth", "virbr", "lo"))]

    def wifi_dev(self):
        return next((d for d in self.devices() if isinstance(d, NM.DeviceWifi)), None)

    def wired_devs(self):
        return [d for d in self.devices() if isinstance(d, NM.DeviceEthernet)]

    def primary(self):
        """(device, kind 'wired'|'wifi'|None, active_connection)"""
        ac = self.c.get_primary_connection()
        if not ac: return None, None, None
        devs = ac.get_devices()
        d = devs[0] if devs else None
        if isinstance(d, NM.DeviceWifi): return d, "wifi", ac
        if isinstance(d, NM.DeviceEthernet): return d, "wired", ac
        return d, "other", ac

    def connectivity(self):
        return self.c.get_connectivity()   # NONE / PORTAL / LIMITED / FULL / UNKNOWN

    def saved_wifi(self):
        out = []
        for conn in self.c.get_connections():
            sw = conn.get_setting_wireless()
            if sw and conn.get_connection_type() == "802-11-wireless":
                out.append(conn)
        return sorted(out, key=lambda c: c.get_id().lower())

    def by_type(self, *types):
        return sorted([c for c in self.c.get_connections() if c.get_connection_type() in types],
                      key=lambda c: c.get_id().lower())

    def active_for(self, conn):
        for ac in self.c.get_active_connections():
            if ac.get_uuid() == conn.get_uuid():
                return ac
        return None

    def activate(self, conn, cb):
        self.c.activate_connection_async(conn, None, None, None,
                                         lambda c, res: self._finish(c.activate_connection_finish, res, cb), None)

    def deactivate(self, ac, cb):
        self.c.deactivate_connection_async(ac, None, lambda c, res: self._finish(c.deactivate_connection_finish, res, cb), None)

    def saved_for_ssid(self, ssid):
        for conn in self.saved_wifi():
            if ssid_text(conn.get_setting_wireless().get_ssid()) == ssid:
                return conn
        return None

    def networks(self):
        """Visible Wi-Fi networks, one per SSID (strongest AP), strongest first."""
        d = self.wifi_dev()
        if not d: return []
        best = {}
        for ap in d.get_access_points():
            name = ssid_text(ap.get_ssid())
            if not name: continue
            if name not in best or ap.get_strength() > best[name].get_strength():
                best[name] = ap
        active = d.get_active_access_point()
        active_ssid = ssid_text(active.get_ssid()) if active else None
        return sorted(best.items(), key=lambda kv: (kv[0] != active_ssid, -kv[1].get_strength()))

    # ---- actions ----
    def set_wifi(self, on, cb=None):
        self.c.dbus_set_property(NM.DBUS_PATH, NM.DBUS_INTERFACE, "WirelessEnabled", GLib.Variant("b", on),
                                 -1, None, lambda c, res: self._finish(c.dbus_set_property_finish, res, cb), None)

    def scan(self):
        d = self.wifi_dev()
        if d and self.c.wireless_get_enabled():
            d.request_scan_async(None, lambda dev, res: self._finish(dev.request_scan_finish, res), None)

    def _finish(self, fn, res, cb=None):
        try:
            fn(res); cb and cb(None)
        except GLib.Error as e:
            cb and cb(e.message)

    def connect_known(self, conn, dev, ap, cb):
        self.c.activate_connection_async(conn, dev, ap.get_path() if ap else None, None,
                                         lambda c, res: self._finish(c.activate_connection_finish, res, cb), None)

    def connect_new(self, dev, ap, password, cb):
        ssid = ssid_text(ap.get_ssid())
        conn = NM.SimpleConnection.new()
        s_con = NM.SettingConnection.new()
        s_con.set_property(NM.SETTING_CONNECTION_ID, ssid)
        s_con.set_property(NM.SETTING_CONNECTION_UUID, str(uuid.uuid4()))
        s_con.set_property(NM.SETTING_CONNECTION_TYPE, "802-11-wireless")
        conn.add_setting(s_con)
        s_wifi = NM.SettingWireless.new()
        s_wifi.set_property(NM.SETTING_WIRELESS_SSID, ap.get_ssid())
        conn.add_setting(s_wifi)
        sec = ap_security(ap)
        if sec in ("psk", "sae"):
            s_sec = NM.SettingWirelessSecurity.new()
            s_sec.set_property(NM.SETTING_WIRELESS_SECURITY_KEY_MGMT, "sae" if sec == "sae" else "wpa-psk")
            s_sec.set_property(NM.SETTING_WIRELESS_SECURITY_PSK, password)
            conn.add_setting(s_sec)
        self.c.add_and_activate_connection_async(
            conn, dev, ap.get_path(), None,
            lambda c, res: self._finish(c.add_and_activate_connection_finish, res, cb), None)

    def disconnect(self, dev, cb=None):
        dev.disconnect_async(None, lambda d, res: self._finish(d.disconnect_finish, res, cb), None)

    def connect_wired(self, dev, cb=None):
        self.c.activate_connection_async(None, dev, None, None,
                                         lambda c, res: self._finish(c.activate_connection_finish, res, cb), None)

    def set_autoconnect(self, conn, on):
        conn.get_setting_connection().set_property(NM.SETTING_CONNECTION_AUTOCONNECT, on)
        conn.commit_changes_async(True, None, lambda c, res: self._finish(c.commit_changes_finish, res), None)

    def forget(self, conn):
        conn.delete_async(None, lambda c, res: self._finish(c.delete_finish, res), None)

    def enable_portal_check(self):
        """Turn on NetworkManager's built-in captive-portal check (it's installed but often off)."""
        if self.c.connectivity_check_get_available() and not self.c.connectivity_check_get_enabled():
            self.c.dbus_set_property(NM.DBUS_PATH, NM.DBUS_INTERFACE, "ConnectivityCheckEnabled",
                                     GLib.Variant("b", True), -1, None, None, None)

    # ---- tailscale ----
    def _tailscale_loop(self):
        while True:
            self.refresh_tailscale()
            time.sleep(20)

    def refresh_tailscale(self):
        if not shutil.which("tailscale"):
            self.tailscale = {}; return
        code, out, _ = run("tailscale", "status", "--json", timeout=4)
        try:
            d = json.loads(out) if out else {}
        except ValueError:
            d = {}
        new = {"state": d.get("BackendState", "Unknown"),
               "ip": next((ip for ip in (d.get("Self") or {}).get("TailscaleIPs", []) if "." in ip), "")}
        if new != self.tailscale:
            self.tailscale = new
            GLib.idle_add(self.changed)

    def set_tailscale(self, on, cb):
        def work():
            code, _o, err = run("tailscale", "up" if on else "down", timeout=20)
            self.refresh_tailscale()
            GLib.idle_add(cb, None if code == 0 else (err.strip() or "failed"))
        threading.Thread(target=work, daemon=True).start()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self._updating = False
        self.busy = {}   # ssid/iface -> status text while connecting
        self.saved_win = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        css = Gtk.CssProvider(); css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.net = Net(self.refresh)
        self.net.enable_portal_check()
        self.build()
        self.setup_tray()
        self.last_conn = None
        self.refresh()

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if "--version" in args:
            cl.print_literal(f"EasyNet {VERSION}\n"); return 0
        if "--quit" in args:
            self.quit(); return 0
        if "--hidden" in args:
            return 0
        self.toggle(); return 0

    # ---- tray ----
    def setup_tray(self):
        self.tray = None
        if XApp is None: return
        t = XApp.StatusIcon(); t.set_name("easynet")
        t.connect("activate", lambda *_: self.toggle())
        self.tray_menu = Gtk.Menu(); t.set_secondary_menu(self.tray_menu)
        self.tray = t

    def status(self):
        """(icon, headline, detail, level 'ok'|'warn'|'bad')"""
        dev, kind, ac = self.net.primary()
        conn = self.net.connectivity()
        if not ac:
            wifi_on = self.net.c.wireless_get_enabled()
            return ("network-offline-symbolic", "Not connected",
                    "Pick a Wi-Fi network below." if wifi_on else "Wi-Fi is off.", "bad")
        name = ac.get_id()
        if kind == "wifi":
            ap = dev.get_active_access_point()
            st = ap.get_strength() if ap else 0
            icon = signal_icon(st)
            detail = f"Wi-Fi · {st}% signal"
        elif kind == "wired":
            icon = "network-wired-symbolic"
            spd = dev.get_speed()
            detail = "Wired" + (f" · {spd // 1000} Gb/s" if spd and spd >= 1000 else f" · {spd} Mb/s" if spd else "")
        else:
            icon, detail = "network-wired-symbolic", "Connected"
        ip = ip4_of(dev)
        if ip: detail += f" · {ip}"
        if conn == NM.ConnectivityState.PORTAL:
            return ("network-error-symbolic", name, "Needs sign-in before the internet works.", "warn")
        if conn in (NM.ConnectivityState.LIMITED, NM.ConnectivityState.NONE):
            return ("network-error-symbolic", name, detail + "\nConnected, but no internet.", "bad")
        return (icon, name, detail, "ok")

    def update_tray(self):
        if not self.tray: return
        icon, head, detail, level = self.status()
        self.tray.set_icon_name(icon)
        ts = self.net.tailscale
        tip = f"{head}\n{detail}" + (f"\nTailscale: {ts['ip']}" if ts.get("state") == "Running" else "")
        self.tray.set_tooltip_text(tip)
        m = self.tray_menu
        for c in m.get_children(): c.destroy()
        def item(label, cb, sensitive=True):
            it = Gtk.MenuItem(label=label); it.set_sensitive(sensitive)
            it.connect("activate", lambda *_: cb()); m.append(it)
        hd = Gtk.MenuItem(label=head); hd.set_sensitive(False); m.append(hd)
        if self.net.connectivity() == NM.ConnectivityState.PORTAL:
            item("Sign in to network…", self.open_portal)
        m.append(Gtk.SeparatorMenuItem())
        if self.net.wifi_dev():
            on = self.net.c.wireless_get_enabled()
            item("Switch back to wired" if on and self.net.wired_devs() else "Turn Wi-Fi off" if on else "Switch to Wi-Fi",
                 lambda: self.use_wifi(not on))
        if self.net.tailscale.get("state") in ("Running", "Stopped"):
            running = self.net.tailscale["state"] == "Running"
            item("Turn Tailscale off" if running else "Turn Tailscale on", lambda: self.toggle_tailscale(not running))
        item("Saved Wi-Fi…", self.open_saved)
        item("Network settings…", self.open_settings)
        m.append(Gtk.SeparatorMenuItem())
        item("Quit EasyNet", self.quit)
        m.show_all()

    # ---- panel ----
    def build(self):
        w = Gtk.Window(application=self, title="Network", decorated=False, resizable=False)
        w.get_style_context().add_class("easynet")
        vis = w.get_screen().get_rgba_visual()
        if vis: w.set_visual(vis)
        w.set_app_paintable(True)
        w.set_keep_above(True); w.set_skip_taskbar_hint(True); w.set_skip_pager_hint(True)
        w.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        w.connect("focus-out-event", lambda *_: (GLib.timeout_add(150, self._maybe_hide), False)[-1])
        self._live = None
        w.connect("map", lambda *_: self._start_live())
        w.connect("unmap", lambda *_: self._stop_live())
        w.connect("key-press-event", lambda _w, ev: (w.hide(), True)[-1] if ev.keyval == Gdk.KEY_Escape else False)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        card.get_style_context().add_class("en-card"); card.set_size_request(360, -1)

        # status block
        st = Gtk.Box(spacing=12); st.get_style_context().add_class("en-status")
        self.st_icon = Gtk.Image(pixel_size=28)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.st_head = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END); self.st_head.get_style_context().add_class("en-title")
        self.st_detail = Gtk.Label(xalign=0, wrap=True, max_width_chars=36); self.st_detail.get_style_context().add_class("en-dim")
        self.st_net = Gtk.Label(xalign=0)
        col.pack_start(self.st_head, False, False, 0); col.pack_start(self.st_detail, False, False, 0)
        col.pack_start(self.st_net, False, False, 0)
        st.pack_start(self.st_icon, False, False, 0); st.pack_start(col, True, True, 0)
        card.pack_start(st, False, False, 0)
        self.portal_btn = Gtk.Button(label="Sign in to network")
        self.portal_btn.get_style_context().add_class("en-portal")
        self.portal_btn.connect("clicked", lambda *_: self.open_portal())
        self.portal_btn.set_no_show_all(True)
        card.pack_start(self.portal_btn, False, False, 0)

        # wired (only shown when there's more than "the one that's connected" to say)
        self.wired_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.pack_start(self.wired_box, False, False, 0)

        # wi-fi
        wh = Gtk.Box()
        wl = Gtk.Label(label="Wi-Fi", xalign=0); wl.get_style_context().add_class("en-section")
        self.wifi_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.wifi_sw.connect("notify::active", self.on_wifi_switch)
        wh.pack_start(wl, True, True, 0); wh.pack_end(self.wifi_sw, False, False, 0)
        card.pack_start(wh, False, False, 0)
        self.wifi_note = Gtk.Label(xalign=0, wrap=True, max_width_chars=44); self.wifi_note.get_style_context().add_class("en-dim")
        card.pack_start(self.wifi_note, False, False, 0)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                max_content_height=260)
        self.wifi_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sc.add(self.wifi_list)
        self.wifi_scroll = sc; sc.set_no_show_all(True)
        card.pack_start(sc, False, False, 0)

        # VPN (Tailscale + saved VPN/WireGuard connections) and phone tethering
        self.vpn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.pack_start(self.vpn_box, False, False, 0)
        self.tether_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.pack_start(self.tether_box, False, False, 0)

        foot = Gtk.Box(spacing=4)
        for label, fn in (("Saved Wi-Fi…", self.open_saved), ("Network settings…", self.open_settings)):
            b = Gtk.Button(label=label, relief=Gtk.ReliefStyle.NONE); b.get_style_context().add_class("en-link")
            b.connect("clicked", lambda _b, fn=fn: (self.win.hide(), fn()))
            foot.pack_start(b, False, False, 0)
        card.pack_start(foot, False, False, 0)
        self.toast = Gtk.Label(xalign=0, wrap=True, max_width_chars=44)
        card.pack_start(self.toast, False, False, 0)
        w.add(card)
        self.win = w

    def _start_live(self):
        """While the panel is open: rescan every 10 s (about the fastest Wi-Fi hardware allows) and
        refresh signal strengths every 3 s. Stops as soon as the panel closes."""
        self._stop_live()
        self._live_ticks = 0
        def tick():
            self._live_ticks += 1
            if self._live_ticks % 3 == 1:
                self.net.scan()
            self.refresh_wifi()
            return True
        self._live = GLib.timeout_add_seconds(3, tick)

    def _stop_live(self):
        if self._live:
            GLib.source_remove(self._live); self._live = None

    def _maybe_hide(self):
        if self.win.get_visible() and not self.win.is_active() and not getattr(self, "_dialog_open", False):
            self.win.hide()
        return False

    def toggle(self):
        if self.win.get_visible():
            self.win.hide(); return
        self.net.scan()
        self.toast.set_text("")
        self.refresh()
        self.win.show_all()
        self.place_near_pointer()
        self.win.present_with_time(Gtk.get_current_event_time() or int(time.time()))

    def place_near_pointer(self):
        disp = Gdk.Display.get_default()
        _s, x, y = disp.get_default_seat().get_pointer().get_position()
        mon = disp.get_monitor_at_point(x, y).get_workarea()
        ww, wh = self.win.get_size()
        nx = min(max(mon.x + 8, x - ww // 2), mon.x + mon.width - ww - 8)
        ny = y - wh - 12 if y - wh - 12 > mon.y else y + 12
        self.win.move(nx, min(max(mon.y + 8, ny), mon.y + mon.height - wh - 8))

    def flash(self, msg, level="bad"):
        self.toast.set_markup(f"<span foreground='{'#e5534b' if level == 'bad' else '#3fb950'}'>"
                              f"{GLib.markup_escape_text(msg)}</span>")
        GLib.timeout_add_seconds(10, lambda: (self.toast.set_text(""), False)[-1])

    def refresh(self):
        if not self.win: return
        self._updating = True
        try:
            icon, head, detail, level = self.status()
            self.st_icon.set_from_icon_name(icon, Gtk.IconSize.DND)
            self.st_head.set_text(head); self.st_detail.set_text(detail)
            conn = self.net.connectivity()
            word = {"ok": "<span foreground='#3fb950'>●</span> Internet OK",
                    "warn": "<span foreground='#e3b341'>●</span> Sign-in needed",
                    "bad": "<span foreground='#e5534b'>●</span> No internet"}[level]
            if conn == NM.ConnectivityState.UNKNOWN and level == "ok":
                word = "<span foreground='#3fb950'>●</span> Connected"
            self.st_net.set_markup(f"<small>{word}</small>")
            self.portal_btn.set_visible(conn == NM.ConnectivityState.PORTAL)
            self.refresh_wired()
            self.refresh_wifi()
            self.refresh_vpn()
            self.refresh_tether()
        finally:
            self._updating = False
        self.notify_changes()
        self.update_tray()

    def refresh_wired(self):
        for c in self.wired_box.get_children(): c.destroy()
        wired = self.net.wired_devs()
        if not wired: return
        h = Gtk.Label(label="Wired", xalign=0); h.get_style_context().add_class("en-section")
        self.wired_box.pack_start(h, False, False, 0)
        for d in wired:
            ac = d.get_active_connection()
            state = d.get_state()
            plugged = d.get_carrier() if hasattr(d, "get_carrier") else True
            name = ac.get_id() if ac else ("Dock Ethernet" if d.get_iface().startswith("enx") else "Ethernet")
            sub = ("Connected" if state == NM.DeviceState.ACTIVATED else
                   "Connecting…" if NM.DeviceState.PREPARE <= state < NM.DeviceState.ACTIVATED else
                   "Cable unplugged" if not plugged else "Not connected")
            b = self._row(name, "network-wired-symbolic", sub, active=state == NM.DeviceState.ACTIVATED)
            if state == NM.DeviceState.ACTIVATED:
                b.set_tooltip_text("Click to disconnect")
                b.connect("clicked", lambda _b, d=d: self.net.disconnect(d, lambda e: e and self.flash(self.explain(e))))
            elif plugged:
                b.set_tooltip_text("Click to connect")
                b.connect("clicked", lambda _b, d=d: self.net.connect_wired(d, lambda e: e and self.flash(self.explain(e))))
            self.wired_box.pack_start(b, False, False, 0)
        self.wired_box.show_all()

    def _switch_row(self, name, icon, sub, active, on_toggle, sensitive=True):
        row = Gtk.Box(spacing=10); row.get_style_context().add_class("en-row")
        row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        nl = Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END); col.pack_start(nl, False, False, 0)
        if sub:
            sl = Gtk.Label(label=sub, xalign=0, ellipsize=Pango.EllipsizeMode.END); sl.get_style_context().add_class("en-dim")
            col.pack_start(sl, False, False, 0)
        row.pack_start(col, True, True, 0)
        sw = Gtk.Switch(active=active, valign=Gtk.Align.CENTER, sensitive=sensitive)
        sw.connect("notify::active", lambda s_, _p: None if self._updating else on_toggle(s_.get_active()))
        row.pack_end(sw, False, False, 0)
        return row

    def _section(self, box, title):
        for c in box.get_children(): c.destroy()
        h = Gtk.Label(label=title, xalign=0); h.get_style_context().add_class("en-section")
        box.pack_start(h, False, False, 0)

    def _ac_state(self, ac):
        if not ac: return "off"
        st = ac.get_state()
        if st == NM.ActiveConnectionState.ACTIVATED: return "on"
        if st in (NM.ActiveConnectionState.ACTIVATING,): return "connecting"
        return "off"

    def refresh_vpn(self):
        ts = self.net.tailscale
        vpns = self.net.by_type("vpn", "wireguard")
        has_ts = ts.get("state") in ("Running", "Stopped", "NeedsLogin", "Starting")
        if not vpns and not has_ts:
            for c in self.vpn_box.get_children(): c.destroy()
            return
        self._section(self.vpn_box, "VPN")
        if has_ts:
            sub = self.busy.get("tailscale") or {"Running": f"On · {ts.get('ip', '')}", "Stopped": "Off", "Starting": "Starting…",
                   "NeedsLogin": "Needs sign-in: run “tailscale up” once"}[ts["state"]]
            self.vpn_box.pack_start(self._switch_row("Tailscale", "network-vpn-symbolic", sub,
                                                     ts["state"] in ("Running", "Starting"), self.toggle_tailscale,
                                                     ts["state"] != "NeedsLogin"), False, False, 0)
        for conn in vpns:
            ac = self.net.active_for(conn)
            state = self._ac_state(ac)
            sub = self.busy.get(conn.get_uuid()) or {"on": "Connected", "connecting": "Connecting…", "off": "Off"}[state]
            self.vpn_box.pack_start(self._switch_row(conn.get_id(), "network-vpn-symbolic", sub, state != "off",
                                                     lambda on, c=conn: self.toggle_conn(c, on, "vpn")), False, False, 0)
        self.vpn_box.show_all()

    def refresh_tether(self):
        teth = self.net.by_type("bluetooth")
        if not teth:
            for c in self.tether_box.get_children(): c.destroy()
            return
        self._section(self.tether_box, "Phone internet")
        for conn in teth:
            ac = self.net.active_for(conn)
            state = self._ac_state(ac)
            sub = self.busy.get(conn.get_uuid()) or {"on": "Connected through your phone", "connecting": "Connecting…",
                                                     "off": "Bluetooth tethering"}[state]
            name = conn.get_id().replace("Bluetooth Tethering — ", "").replace(" Network", "")
            self.tether_box.pack_start(self._switch_row(name, "phone-symbolic", sub, state != "off",
                                                        lambda on, c=conn: self.toggle_conn(c, on, "tether")), False, False, 0)
        self.tether_box.show_all()

    def toggle_conn(self, conn, on, kind):
        uid = conn.get_uuid()
        def done(err):
            self.busy.pop(uid, None)
            if err: self.flash(f"{conn.get_id()}: " + self.explain(err, kind))
            self.refresh()
        if on:
            self.busy[uid] = "Connecting…"
            self.net.activate(conn, done)
        else:
            ac = self.net.active_for(conn)
            if ac:
                self.busy[uid] = "Disconnecting…"
                self.net.deactivate(ac, done)
        self.refresh()

    def explain(self, err, kind=""):
        e = err.lower()
        if "not authorized" in e or "permission" in e:
            return "not allowed from here. Reopen EasyNet from the app menu (or log out and back in)."
        if kind == "tether" and any(k in e for k in ("bluetooth", "no suitable device", "not available", "timeout", "failed")):
            return "couldn't reach the phone. Turn on Bluetooth tethering on the phone and keep it nearby."
        if "secrets" in e:
            return "needs a password or sign-in."
        return err.split(":")[-1].strip() or "failed"

    def _row(self, name, icon, sub, active=False, extra_icon=None):
        b = Gtk.Button(relief=Gtk.ReliefStyle.NONE); ctx = b.get_style_context(); ctx.add_class("en-row")
        if active: ctx.add_class("active")
        row = Gtk.Box(spacing=10)
        row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
        nl = Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END); nl.get_style_context().add_class("en-name")
        row.pack_start(nl, True, True, 0)
        if extra_icon:
            row.pack_start(Gtk.Image.new_from_icon_name(extra_icon, Gtk.IconSize.MENU), False, False, 0)
        sl = Gtk.Label(label=sub, xalign=1); sl.get_style_context().add_class("en-dim")
        row.pack_start(sl, False, False, 0)
        if active:
            row.pack_start(Gtk.Image.new_from_icon_name("object-select-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        b.add(row)
        return b

    def _clear_wifi_list(self):
        self._wifi_sig = None
        for c in self.wifi_list.get_children(): c.destroy()

    def refresh_wifi(self):
        d = self.net.wifi_dev()
        hw = self.net.c.wireless_hardware_get_enabled()
        on = self.net.c.wireless_get_enabled()
        self.wifi_sw.set_active(on); self.wifi_sw.set_sensitive(bool(d) and hw)
        docked = any(w.get_state() == NM.DeviceState.ACTIVATED and w.get_iface().startswith("enx")
                     for w in self.net.wired_devs())
        if not d:
            self._clear_wifi_list()
            self.wifi_note.set_text("No Wi-Fi adapter found."); return
        if not hw:
            self._clear_wifi_list()
            self.wifi_note.set_text("Wi-Fi is switched off by the hardware (airplane-mode key)."); return
        if not on:
            self.wifi_note.set_text("Off while docked: Wi-Fi turns itself back on when you unplug the dock."
                                    if docked and os.path.exists(DOCK_SCRIPT) else "Wi-Fi is off.")
            self._clear_wifi_list(); self.wifi_scroll.hide()
            return
        nets = self.net.networks() if on and d and hw else []
        self.wifi_scroll.set_visible(bool(nets))
        if nets: self.wifi_scroll.get_child().show_all()
        if not nets:
            self.wifi_note.set_text("Looking for networks…")
            self._clear_wifi_list()
            return
        self.wifi_note.set_text("")
        active = d.get_active_access_point()
        active_ssid = ssid_text(active.get_ssid()) if active else None
        # Only redraw when something visible changed, so the list doesn't flicker under the mouse
        # while the panel is open and refreshing.
        sig = tuple((ssid, signal_icon(ap.get_strength()), ap_security(ap), bool(self.net.saved_for_ssid(ssid)),
                     ssid == active_ssid and d.get_state() == NM.DeviceState.ACTIVATED, self.busy.get(ssid))
                    for ssid, ap in nets[:12])
        if sig == getattr(self, "_wifi_sig", None):
            return
        self._wifi_sig = sig
        for c in self.wifi_list.get_children(): c.destroy()
        for ssid, ap in nets[:12]:
            sec = ap_security(ap)
            saved = self.net.saved_for_ssid(ssid)
            is_active = ssid == active_ssid and d.get_state() == NM.DeviceState.ACTIVATED
            sub = self.busy.get(ssid) or ("Connected" if is_active else "Saved" if saved else
                                          "Open" if sec == "open" else "")
            b = self._row(ssid, signal_icon(ap.get_strength()), sub, active=is_active,
                          extra_icon=None if sec == "open" else "changes-prevent-symbolic")
            b.set_tooltip_text(f"{ap.get_strength()}% signal · " + {"open": "no password (others can see your traffic)",
                               "psk": "password protected", "sae": "password protected (WPA3)",
                               "enterprise": "company login", "wep": "old, weak security"}[sec])
            b.connect("clicked", lambda _b, ssid=ssid, ap=ap, saved=saved, is_active=is_active:
                      self.on_network(ssid, ap, saved, is_active))
            self.wifi_list.pack_start(b, False, False, 0)
        self.wifi_list.show_all()

    # ---- actions ----
    def on_wifi_switch(self, sw, _p):
        if self._updating: return
        self.use_wifi(sw.get_active())

    def use_wifi(self, on):
        """Wi-Fi on = switch over to Wi-Fi: radio on, wired off, scan and list networks.
        Wi-Fi off = back to wired: radio off, reconnect any plugged-in cable."""
        def after(err):
            if err:
                self.flash("Wi-Fi: " + self.explain(err)); self.refresh(); return
            if on:
                for d in self.net.wired_devs():
                    if d.get_state() >= NM.DeviceState.PREPARE:
                        self.net.disconnect(d, lambda e: e and self.flash(self.explain(e)))
                self.wifi_note.set_text("Looking for networks…")
                # The radio needs a moment before it accepts scan requests; ask a few times.
                for delay in (1500, 4000, 9000):
                    GLib.timeout_add(delay, lambda: (self.net.scan(), self.refresh(), False)[-1])
            else:
                for d in self.net.wired_devs():
                    if d.get_carrier() and d.get_state() < NM.DeviceState.PREPARE:
                        self.net.connect_wired(d, lambda e: e and self.flash(self.explain(e)))
            self.refresh()
        self.net.set_wifi(on, after)

    def on_network(self, ssid, ap, saved, is_active):
        d = self.net.wifi_dev()
        if is_active:
            self.net.disconnect(d, lambda e: e and self.flash(self.explain(e))); return
        def done(err):
            self.busy.pop(ssid, None)
            if err:
                self.flash(f"Couldn't join {ssid}: " + ("wrong password?" if "secrets" in err.lower() or "802-1x" in err.lower()
                                                        else err.split(":")[-1].strip()))
            self.refresh()
        sec = ap_security(ap)
        if saved:
            self.busy[ssid] = "Connecting…"; self.refresh()
            self.net.connect_known(saved, d, ap, done)
        elif sec == "open":
            self.busy[ssid] = "Connecting…"; self.refresh()
            self.net.connect_new(d, ap, None, done)
        elif sec in ("psk", "sae"):
            pw = self.ask_password(ssid)
            if pw:
                self.busy[ssid] = "Connecting…"; self.refresh()
                self.net.connect_new(d, ap, pw, done)
        else:
            self.flash(f"{ssid} needs a company/enterprise login. Opening the full network editor.", "bad")
            subprocess.Popen(["nm-connection-editor"], start_new_session=True)

    def ask_password(self, ssid):
        self._dialog_open = True
        dlg = Gtk.Dialog(title=f"Join {ssid}", transient_for=self.win, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Join", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area(); box.set_spacing(8); box.set_border_width(14)
        box.pack_start(Gtk.Label(label=f"Password for “{ssid}”", xalign=0), False, False, 0)
        e = Gtk.Entry(visibility=False, activates_default=True, input_purpose=Gtk.InputPurpose.PASSWORD)
        e.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
        e.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Show password")
        e.connect("icon-press", lambda en, *_: en.set_visibility(not en.get_visibility()))
        box.pack_start(e, False, False, 0)
        dlg.show_all()
        ok = dlg.run() == Gtk.ResponseType.OK
        pw = e.get_text()
        dlg.destroy(); self._dialog_open = False
        if ok and len(pw) < 8:
            self.flash("Wi-Fi passwords are at least 8 characters."); return None
        return pw if ok else None

    def toggle_tailscale(self, on):
        self.busy["tailscale"] = "Turning on…" if on else "Turning off…"
        def done(err):
            self.busy.pop("tailscale", None)
            if err: self.flash("Tailscale: " + self.explain(err))
            self.refresh()
        self.net.set_tailscale(on, done)

    def open_portal(self):
        Gio.AppInfo.launch_default_for_uri(PORTAL_PROBE, None)
        self.win.hide()

    def open_settings(self):
        exe = shutil.which("cinnamon-settings")
        subprocess.Popen([exe, "network"] if exe else ["nm-connection-editor"], start_new_session=True)

    def notify_changes(self):
        """One-line heads-up when the connection or internet state changes (not on every refresh)."""
        icon, head, detail, level = self.status()
        key = (head, level)
        if self.last_conn is not None and key != self.last_conn and level == "warn":
            try:
                gi.require_version("Notify", "0.7")
                from gi.repository import Notify
                Notify.init("EasyNet")
                n = Notify.Notification.new(f"{head} needs sign-in", "Open the login page to finish connecting.",
                                            "network-wireless")
                n.add_action("open", "Sign in", lambda *_: self.open_portal())
                self._note = n; n.show()
            except Exception:
                pass
        self.last_conn = key

    # ---- saved wi-fi ----
    def open_saved(self):
        if self.saved_win:
            self.saved_win.present(); return
        w = Gtk.Window(title="Saved Wi-Fi", application=self)
        w.set_default_size(440, 520)
        hb = Gtk.HeaderBar(show_close_button=True, title="Saved Wi-Fi"); w.set_titlebar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=14)
        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=52)
        note.set_markup("<small>Auto-join connects by itself when the network is nearby. Turning it off for "
                        "hotel, guest and other public networks is safer; you can still join them by hand.</small>")
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        fr = Gtk.Frame(); lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.set_header_func(lambda r, before: r.set_header(Gtk.Separator() if before else None))
        fr.add(lb); sc.add(fr); box.pack_start(sc, True, True, 0)
        def fill():
            for c in lb.get_children(): c.destroy()
            for conn in self.net.saved_wifi():
                ssid = ssid_text(conn.get_setting_wireless().get_ssid()) or conn.get_id()
                sec = conn.get_setting_wireless_security()
                row = Gtk.Box(spacing=10, margin=10)
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                col.pack_start(Gtk.Label(label=ssid, xalign=0, ellipsize=Pango.EllipsizeMode.END), False, False, 0)
                sub = Gtk.Label(xalign=0); sub.get_style_context().add_class("dim-label")
                sub.set_markup("<small>" + ("Password protected" if sec else "Open network") + "</small>")
                col.pack_start(sub, False, False, 0)
                row.pack_start(col, True, True, 0)
                lab = Gtk.Label(label="Auto-join"); lab.get_style_context().add_class("dim-label")
                sw = Gtk.Switch(active=conn.get_setting_connection().get_autoconnect(), valign=Gtk.Align.CENTER)
                sw.connect("notify::active", lambda s, _p, c=conn: self.net.set_autoconnect(c, s.get_active()))
                fg = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER)
                fg.connect("clicked", lambda _b, c=conn, n=ssid: self.confirm_forget(w, c, n, fill))
                row.pack_end(fg, False, False, 0); row.pack_end(sw, False, False, 0); row.pack_end(lab, False, False, 0)
                r = Gtk.ListBoxRow(activatable=False); r.add(row); lb.add(r)
            lb.show_all()
        fill()
        w.add(box)
        w.connect("destroy", lambda *_: setattr(self, "saved_win", None))
        self.saved_win = w; w.show_all()

    def confirm_forget(self, parent, conn, name, refill):
        d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text=f"Forget “{name}”?")
        d.format_secondary_text("Its saved password is removed. You can join again later.")
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Forget", Gtk.ResponseType.OK)
        d.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        if d.run() == Gtk.ResponseType.OK:
            self.net.forget(conn)
            GLib.timeout_add(600, lambda: (refill(), False)[-1])
        d.destroy()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
