"""TelemetryDrawer — panel de monitoreo, consola y post-mortem"""
from __future__ import annotations

import flet as ft

# Design tokens
BG_PANEL     = "#FF080808"
BG_HOVER     = "#FF1E1E28"
BORDER_LIGHT = "#FF3D4455"
BORDER_MUTED = "#FF2A3040"
ACCENT       = "#FF4F8BFF"
SUCCESS      = "#FF00D084"
ERROR_COL    = "#FFFF4444"
TEXT_PRIMARY = "#FFF2F6FF"
TEXT_MUTED   = "#FF7A8499"
TEXT_DIM     = "#FF3D4455"

_TELE_HANDLE_H   = 35
_TELE_PANEL_H    = 300
_TELE_DOCKED_MIN = 700
_TELE_ANIM       = ft.Animation(400, ft.AnimationCurve.DECELERATE)


class TelemetryDrawer:
    """
    DOCKED   (window >= 700 px): panel integrado en la columna del sidebar.
    OVERLAY  (window < 700 px): handle de 35 px anclado al fondo del sidebar;
      al hacer clic el panel se expande hacia la derecha via page.overlay.
    """

    _TAB_LABELS  = ["Monitor", "Consola", "Post-Mortem"]
    _DEFAULT_TAB = 1  # Consola

    def __init__(self, page: ft.Page, sidebar_width: int = 300) -> None:
        self.page          = page
        self.sidebar_width = sidebar_width
        self._open         = False

        self._d_log   = ft.ListView(spacing=0, expand=True)
        self._o_log   = ft.ListView(spacing=0, expand=True)
        self._d_pm    = ft.ListView(spacing=0, expand=True)
        self._o_pm    = ft.ListView(spacing=0, expand=True)
        self._d_pm_ph = ft.Text("Sin errores registrados", size=10, color=TEXT_DIM,
                                font_family="IBM Plex Sans", opacity=0.6,
                                text_align=ft.TextAlign.CENTER)
        self._o_pm_ph = ft.Text("Sin errores registrados", size=10, color=TEXT_DIM,
                                font_family="IBM Plex Sans", opacity=0.6,
                                text_align=ft.TextAlign.CENTER)
        self._d_cnts = self._mk_cnts()
        self._o_cnts = self._mk_cnts()

        self._last_failed: list = []
        self._pm_meta:     dict = {}

        d_body, self._d_panels, self._d_tab_btns = self._build_body(
            self._d_cnts, self._d_log, self._d_pm, self._d_pm_ph)
        o_body, self._o_panels, self._o_tab_btns = self._build_body(
            self._o_cnts, self._o_log, self._o_pm, self._o_pm_ph)

        self.container = ft.Container(
            content=d_body,
            bgcolor=BG_PANEL,
            border=ft.Border.all(0.8, BORDER_LIGHT),
            border_radius=8,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            height=_TELE_PANEL_H,
            visible=True,
        )

        self._overlay_panel = ft.Container(
            content=o_body,
            left=sidebar_width, bottom=0,
            width=0, height=_TELE_PANEL_H,
            bgcolor=BG_PANEL,
            border=ft.Border(
                top=ft.BorderSide(0.8, BORDER_LIGHT),
                right=ft.BorderSide(0.8, BORDER_LIGHT),
            ),
            border_radius=ft.BorderRadius.only(top_right=12),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            animate_size=_TELE_ANIM,
        )

        self._arrow = ft.Icon(icon=ft.Icons.ARROW_FORWARD, color=TEXT_DIM, size=14)
        self.handle = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(width=15, height=2, bgcolor=TEXT_DIM, border_radius=2, opacity=0.5),
                    ft.Container(width=5),
                    ft.Container(width=15, height=2, bgcolor=TEXT_DIM, border_radius=2, opacity=0.5),
                    ft.Container(width=6),
                    self._arrow,
                ],
                spacing=0,
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=80, height=_TELE_HANDLE_H,
            bottom=0, left=0,
            bgcolor=BG_PANEL,
            border_radius=ft.BorderRadius.only(top_left=12, top_right=12),
            border=ft.Border(
                top=ft.BorderSide(0.8, BORDER_LIGHT),
                left=ft.BorderSide(0.8, BORDER_LIGHT),
                right=ft.BorderSide(0.8, BORDER_LIGHT),
            ),
            on_click=self._toggle,
            ink=True,
            alignment=ft.Alignment.CENTER,
            visible=False,
        )

    def _mk_cnts(self) -> dict:
        def _t(color):
            return ft.Text("—", size=11, color=color, font_family="IBM Plex Sans",
                           weight=ft.FontWeight.W_600, opacity=1.0)
        return {
            "detected":   _t(TEXT_MUTED),
            "candidates": _t(TEXT_MUTED),
            "processed":  _t(ACCENT),
            "confirmed":  _t(SUCCESS),
            "rejected":   _t(ERROR_COL),
        }

    def _build_body(self, cnts, log_list, pm_list, pm_ph) -> tuple:
        def _crow(label, val_node):
            return ft.Row([
                ft.Text(label, size=9, color=TEXT_DIM, font_family="IBM Plex Sans", opacity=1.0),
                ft.Container(expand=True),
                val_node,
            ], spacing=0)

        panel_monitor = ft.Container(
            content=ft.Column([
                _crow("Detectadas",     cnts["detected"]),
                _crow("Candidatas",     cnts["candidates"]),
                _crow("Procesadas",     cnts["processed"]),
                _crow("Confirmadas",    cnts["confirmed"]),
                _crow("Rechazadas API", cnts["rejected"]),
            ], spacing=5),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            expand=True, visible=(self._DEFAULT_TAB == 0),
        )
        panel_consola = ft.Container(
            content=log_list,
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            expand=True, visible=(self._DEFAULT_TAB == 1),
        )
        export_btn = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.DOWNLOAD_OUTLINED, size=11, color=ACCENT),
                ft.Text("Exportar a TXT", size=10, color=ACCENT, font_family="IBM Plex Sans", opacity=1.0),
            ], spacing=4, tight=True),
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            border=ft.Border.all(0.8, ACCENT),
            on_click=lambda _: self._do_export(),
            ink=True,
        )
        panel_postmortem = ft.Container(
            content=ft.Column([
                ft.Stack([
                    ft.Container(content=pm_ph, alignment=ft.Alignment.CENTER, expand=True),
                    pm_list,
                ], expand=True),
                ft.Container(
                    content=ft.Row([export_btn], alignment=ft.MainAxisAlignment.END),
                    padding=ft.Padding.only(right=4, bottom=4, top=2),
                ),
            ], spacing=0, expand=True),
            padding=ft.Padding.symmetric(horizontal=6, vertical=6),
            expand=True, visible=(self._DEFAULT_TAB == 2),
        )
        panels = [panel_monitor, panel_consola, panel_postmortem]

        tab_btns: list[ft.Container] = []
        for i, label in enumerate(self._TAB_LABELS):
            active = (i == self._DEFAULT_TAB)
            btn = ft.Container(
                content=ft.Text(
                    label, size=10,
                    color=TEXT_PRIMARY if active else TEXT_DIM,
                    font_family="IBM Plex Sans",
                    weight=ft.FontWeight.W_600 if active else ft.FontWeight.W_400,
                    opacity=1.0,
                ),
                padding=ft.Padding.symmetric(horizontal=8, vertical=5),
                border_radius=ft.BorderRadius.only(top_left=5, top_right=5),
                bgcolor=BG_HOVER if active else ft.Colors.TRANSPARENT,
                border=ft.Border(bottom=ft.BorderSide(
                    1.5 if active else 0, ACCENT if active else ft.Colors.TRANSPARENT
                )),
                ink=True,
            )
            tab_btns.append(btn)

        def _make_on_click(idx, _panels, _btns):
            def _on_click(_):
                self._switch_to_tab(idx, _panels, _btns)
                self.page.update()
            return _on_click

        for i, btn in enumerate(tab_btns):
            btn.on_click = _make_on_click(i, panels, tab_btns)

        tab_row = ft.Container(
            content=ft.Row(tab_btns, spacing=2),
            border=ft.Border(bottom=ft.BorderSide(0.8, BORDER_MUTED)),
            padding=ft.Padding.only(left=6, right=6, top=4, bottom=0),
        )
        body = ft.Column(controls=[tab_row, ft.Stack(controls=panels, expand=True)], spacing=0, expand=True)
        return body, panels, tab_btns

    def _switch_to_tab(self, idx: int, panels: list, btns: list) -> None:
        for j, (p, b) in enumerate(zip(panels, btns)):
            is_sel = (j == idx)
            p.visible        = is_sel
            b.bgcolor        = BG_HOVER if is_sel else ft.Colors.TRANSPARENT
            b.content.color  = TEXT_PRIMARY if is_sel else TEXT_DIM
            b.content.weight = ft.FontWeight.W_600 if is_sel else ft.FontWeight.W_400
            b.border = ft.Border(bottom=ft.BorderSide(
                1.5 if is_sel else 0, ACCENT if is_sel else ft.Colors.TRANSPARENT
            ))

    def show_postmortem(self) -> None:
        self._switch_to_tab(2, self._d_panels, self._d_tab_btns)
        self._switch_to_tab(2, self._o_panels, self._o_tab_btns)
        h = self.page.height or self.page.window.height or 720
        if h < _TELE_DOCKED_MIN and not self._open:
            self._open_overlay()
        else:
            self.page.update()

    def clear_postmortem(self) -> None:
        self._last_failed = []
        self._pm_meta     = {}
        for lst, ph in ((self._d_pm, self._d_pm_ph), (self._o_pm, self._o_pm_ph)):
            lst.controls.clear()
            ph.visible = True

    def _snack(self, msg: str) -> None:
        s = ft.SnackBar(content=ft.Text(msg, font_family="IBM Plex Sans", size=12, opacity=1.0),
                        bgcolor=BG_PANEL, duration=3500)
        self.page.show_dialog(s)

    def _do_export(self) -> None:
        import datetime as _dt
        tracks = self._last_failed
        if not tracks:
            self._snack("No hay datos de Post-Mortem para exportar.")
            return
        meta     = self._pm_meta
        log_path = "transfer_failed_report.txt"
        lines    = [
            "# MelomaniacPass — Reporte Post-Mortem\n",
            f"# Fecha: {_dt.datetime.now().isoformat()}\n",
            f"# Destino: {meta.get('destination', '?')}\n",
            f"# Confirmadas: {meta.get('confirmed', 0)} / Detectadas: {meta.get('detected', 0)}\n\n",
        ]
        for t in tracks:
            reason = (getattr(t, "failure_reason", "") or "").strip()
            lines.append(f"{t.name} | {t.artist} | {reason or '—'}\n")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            self._snack(f"✓ Reporte guardado → {log_path}")
        except OSError as exc:
            self._snack(f"Error al exportar: {exc}")

    def sync_mode(self) -> None:
        h      = self.page.height or self.page.window.height or 720
        docked = h >= _TELE_DOCKED_MIN
        if docked:
            self.container.visible = True
            self.handle.visible    = False
            if self._open:
                self._close_overlay()
        else:
            self.container.visible = False
            self.handle.visible    = True

    def _toggle(self, _=None) -> None:
        if self._open:
            self._close_overlay()
        else:
            self._open_overlay()

    def _open_overlay(self) -> None:
        self._open = True
        target_w   = max(400, int((self.page.window.width or 1200) * 0.4))
        if self._overlay_panel not in self.page.overlay:
            self.page.overlay.append(self._overlay_panel)
        self._overlay_panel.width = target_w
        self._arrow.icon = ft.Icons.ARROW_BACK
        self.page.update()

    def _close_overlay(self) -> None:
        self._open                = False
        self._overlay_panel.width = 0
        self._arrow.icon          = ft.Icons.ARROW_FORWARD
        self.page.update()

    def update_counters(self, detected: int, candidates: int, processed: int,
                        confirmed: int, rejected: int) -> None:
        def _f(n): return str(n) if n else "—"
        for c in (self._d_cnts, self._o_cnts):
            c["detected"].value   = _f(detected)
            c["candidates"].value = _f(candidates)
            c["processed"].value  = _f(processed)
            c["confirmed"].value  = _f(confirmed)
            c["rejected"].value   = _f(rejected)

    def update_log(self, log_lines: list[str]) -> None:
        for lst in (self._d_log, self._o_log):
            lst.controls.clear()
            for line in log_lines[-80:]:
                col = (SUCCESS if "[SUCCESS]" in line else ERROR_COL if "[ERROR]" in line else TEXT_MUTED)
                lst.controls.append(
                    ft.Text(f"› {line}", size=9, color=col, font_family="IBM Plex Sans", opacity=1.0)
                )

    def update_postmortem(self, failed_tracks, *, destination="", confirmed=0, detected=0) -> None:
        self._last_failed = list(failed_tracks)
        self._pm_meta     = dict(destination=destination, confirmed=confirmed, detected=detected)
        has = bool(self._last_failed)
        for lst, ph in ((self._d_pm, self._d_pm_ph), (self._o_pm, self._o_pm_ph)):
            lst.controls.clear()
            ph.visible = not has
            for t in self._last_failed:
                reason = (getattr(t, "failure_reason", "") or "").strip()
                label  = f"✗  {t.name}  —  {t.artist}"
                if reason:
                    label += f"  ·  {reason[:48]}"
                lst.controls.append(
                    ft.Text(label, size=9, color=ERROR_COL, font_family="IBM Plex Sans", opacity=1.0)
                )
