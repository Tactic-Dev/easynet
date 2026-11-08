# EasyNet 1.2.1

A simple, clear network tray icon for Linux desktops (GTK3 + NetworkManager/libnm). Replaces Cinnamon's network applet.

- **Tray icon:** wired, Wi-Fi signal bars, or a warning when there's no internet or a hotel login page is waiting. The tooltip shows the connection, IP and Tailscale address.
- **Panel:**
  - Status: name, link speed or signal, IP, and internet status (OK / sign-in needed / no internet).
  - A **Sign in to network** button for captive portals, plus a notification when one is detected.
  - Wired devices: click to connect or disconnect.
  - Wi-Fi: on/off switch and networks with signal and lock icons. Click to join; a password box appears when needed (WPA2/WPA3).
  - Tailscale on/off (needs `tailscale set --operator=$USER`).
  - Saved Wi-Fi: per-network auto-join switch and Forget.
- Docker bridges, veth, loopback and tun devices are hidden.
- Explains "Wi-Fi off while docked" when a dispatcher script turns Wi-Fi off on dock Ethernet.

Run: `python3 easynet.py` (`--hidden`, `--show`, `--quit`). Needs python3-gi, gir1.2-nm-1.0, gir1.2-gtk-3.0, optional gir1.2-xapp-1.0.
Portal detection needs NetworkManager's connectivity check, e.g. `/etc/NetworkManager/conf.d/90-easynet-connectivity.conf` with `[connectivity] enabled=true`.
