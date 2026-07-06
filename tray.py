# -*- coding: utf-8 -*-
"""Run the SpliceScout web server from the system tray (no blocking console window).

launch_Win.bat starts this with pythonw (windowless); a tray icon gives Open / Quit. If the tray
libraries (pystray + Pillow) aren't importable, it falls back to the plain console server, so this is
always safe to run. Reuses server.py's instance-tag + free-port machinery (so the tray app still gets
its own cluster JOB_TAG -- the $SPLICESCOUT_INSTANCE name from the launcher, else an auto sraN -- plus
its own port, exactly like `python server.py`).
"""
import os
import threading
import webbrowser


def _start_server():
    """Claim this instance's slot, bind a free port, serve in a daemon thread. Returns (server, httpd, url, tag)."""
    import atexit
    import server
    os.chdir(os.path.dirname(os.path.abspath(server.__file__)))
    try:                                # exon ref ships gzip'd -> recreate the plain-text copy (idempotent)
        import ensure_refs
        ensure_refs.ensure_ref_files()
    except Exception:
        pass
    server._INSTANCE_TAG, server._INSTANCE_LOCK_PATH = server._claim_instance_slot(server._resolve_instance_name())
    atexit.register(server._release_instance_slot)
    httpd, port = server._bind_server("127.0.0.1", 8765)
    url = "http://127.0.0.1:%d/" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return server, httpd, url, server._INSTANCE_TAG


def _icon_image():
    from PIL import Image, ImageDraw, ImageFont
    N = 128                                            # render large; the tray downsamples -> crisp
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))      # transparent bg -> just the rounded square shows
    d = ImageDraw.Draw(img)
    m = 10
    try:
        d.rounded_rectangle([m, m, N - m, N - m], radius=26, fill=(91, 140, 255, 255))
    except Exception:
        d.rectangle([m, m, N - m, N - m], fill=(91, 140, 255, 255))
    # A LARGE, centered "S". The PIL default font is ~6px and renders as a dot at tray size, so load a
    # real TrueType (try a few common Windows/Linux bold faces; fall back to the bitmap font only if none).
    font = None
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, 84)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    try:                                               # center the glyph by its bounding box
        l, t, r, b = d.textbbox((0, 0), "S", font=font)
        d.text(((N - (r - l)) / 2 - l, (N - (b - t)) / 2 - t), "S", fill=(255, 255, 255, 255), font=font)
    except Exception:
        d.text((N / 2 - 22, N / 2 - 44), "S", fill=(255, 255, 255, 255), font=font)
    return img


def main():
    try:
        import pystray            # noqa: F401
        from PIL import Image     # noqa: F401
    except Exception:
        import server             # no tray libs -> normal console server
        server.main()
        return

    import pystray
    server, httpd, url, tag = _start_server()
    # Hand the URL to the launcher (launch_Win.bat) by writing it next to this script. Under pythonw the
    # in-process webbrowser.open() returns True but silently fails to surface a tab on some Windows setups,
    # so the .bat reads this file and opens the browser from its reliable console context. When this script
    # is run DIRECTLY (not via the launcher), we still open the browser ourselves.
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_last_url.txt"),
                  "w", encoding="utf-8") as uf:
            uf.write(url)
    except Exception:
        pass
    if not os.environ.get("SPLICESCOUT_OPENED_BY_LAUNCHER"):
        webbrowser.open(url)

    def do_open(icon=None, item=None):
        webbrowser.open(url)

    def do_quit(icon, item):
        try:
            httpd.shutdown()
        except Exception:
            pass
        server._release_instance_slot()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open SpliceScout (%s)" % tag, do_open, default=True),
        pystray.MenuItem("Open in browser", do_open),
        pystray.MenuItem("Quit", do_quit),
    )
    pystray.Icon("SpliceScout", _icon_image(), "SpliceScout %s  %s" % (tag, url), menu).run()


if __name__ == "__main__":
    main()
