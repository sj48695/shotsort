#!/usr/bin/env python3
"""shotsort 데스크탑 앱 (NiceGUI native) — 썸네일 격자로 보고 체크해서 일괄 휴지통.

실행:
  .venv/bin/python3 app.py          # 독립 앱 창(native)으로 뜸
  SHOTSORT_BROWSER=1 .venv/bin/python3 app.py   # 브라우저 탭으로 뜸
  SHOTSORT_DEV=1 .venv/bin/python3 app.py        # 개발: 파일변경 자동 리로드(브라우저)

엔진(engine.py)을 그대로 재사용한다. 썸네일은 engine.thumbnail_uri 로 만든
data-URI 라 별도 정적 파일 서버가 필요 없다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from nicegui import run, ui

import engine


@ui.page("/")
def index():
    # 현재 렌더된 카드의 체크박스 핸들 (경로 → checkbox) = 선택 상태의 단일 출처.
    checks: dict[str, "ui.checkbox"] = {}
    # 스캔 진행률(워커 스레드가 갱신, UI 타이머가 읽음)
    progress = {"i": 0, "total": 0, "running": False}

    ui.label("shotsort — 스크린샷 정리").classes("text-2xl font-bold")
    ui.label(
        "스크린샷을 프로젝트별로 묶고, 지워도 되는 것을 체크해서 한꺼번에 휴지통으로 보냅니다 (복구 가능)."
    ).classes("text-sm text-gray-500")

    # ── 업데이트 알림 배너 (기본 숨김, 로드 시 백그라운드 체크) ───────────────
    update_banner = ui.row().classes(
        "w-full items-center gap-3 p-2 rounded"
    ).style("background:#fff3cd")
    update_banner.visible = False
    with update_banner:
        update_lbl = ui.label().classes("text-sm")
        ui.space()
        update_btn = ui.button("업데이트", icon="system_update").props("dense")
        ui.button("나중에", on_click=lambda: update_banner.set_visibility(False)).props("flat dense")

    # ── 스캔 컨트롤 ──────────────────────────────────────────────────────────
    has_key = engine.has_api_key()
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-3 w-full"):
            path_in = ui.input("스캔 경로", value=str(engine.DEFAULT_SCAN_DIR)).classes("grow")
            local_sw = ui.switch("로컬 모드(무료)", value=not has_key)
            img_sw = ui.switch("썸네일도 전송", value=False)
            scan_btn = ui.button("스캔", icon="search")
        mode_lbl = ui.label().classes("text-xs text-gray-500")

        def refresh_mode():
            use_llm = engine.resolve_mode(local_sw.value)
            if use_llm:
                mode_lbl.text = "모드: Claude 분류 (claude-opus-4-8). '썸네일도 전송' 켜면 정확도↑ 비용↑."
                img_sw.enable()
            else:
                why = "로컬 강제" if local_sw.value else "ANTHROPIC_API_KEY 없음"
                mode_lbl.text = f"모드: 로컬 휴리스틱 ({why}) — 무료·오프라인, 정확도는 낮음."
                img_sw.disable()

        local_sw.on_value_change(lambda _: refresh_mode())
        refresh_mode()
        prog_lbl = ui.label().classes("text-xs text-primary")

    # ── 통계 + 일괄 액션 ─────────────────────────────────────────────────────
    with ui.row().classes("items-center gap-4 w-full"):
        stats_lbl = ui.label().classes("text-sm")
        sel_lbl = ui.label("선택 0개").classes("text-sm text-primary")
        ui.space()
        trash_sel_btn = ui.button("선택 항목 휴지통으로", icon="delete", color="red")
        refresh_btn = ui.button("새로고침", icon="refresh").props("flat")

    groups_box = ui.column().classes("w-full gap-2")

    # ── 렌더링 ──────────────────────────────────────────────────────────────
    def update_stats():
        s = engine.stats()
        stats_lbl.text = (
            f"이미지 {s.total}개 · 그룹 {s.groups}개 · 삭제후보 {s.deletable}개({engine.human_mb(s.deletable_bytes)})"
        )

    def selected_paths() -> list[str]:
        return [p for p, cb in checks.items() if cb.value]

    def update_sel():
        n = len(selected_paths())
        sel_lbl.text = f"선택 {n}개"
        trash_sel_btn.set_enabled(bool(n))

    def render_groups():
        checks.clear()
        update_sel()
        groups = engine.list_groups()
        groups_box.clear()
        with groups_box:
            if not groups:
                ui.label("분석된 이미지가 없습니다. 경로를 정하고 '스캔'을 누르세요.").classes(
                    "text-gray-500"
                )
                return
            for g, items in groups.items():
                dele = sum(1 for it in items if it["deletable"])
                title = f"{g}  ({len(items)}개" + (f", 🗑 {dele}" if dele else "") + ")"
                # 기본은 접힘 — 삭제후보 그룹과 큰 그룹(5장+)만 펼쳐서 노이즈를 줄인다.
                expand = (g == engine.CLEANUP_GROUP) or len(items) >= 5
                with ui.expansion(title, value=expand).classes("w-full border rounded"):
                    paths = [it["path"] for it in items]
                    with ui.row().classes("gap-2 mb-2 items-center"):
                        ui.button(
                            "이 그룹 전체선택",
                            on_click=lambda _, ps=paths: select_paths(ps, True),
                        ).props("flat dense")
                        ui.button(
                            "해제", on_click=lambda _, ps=paths: select_paths(ps, False)
                        ).props("flat dense")
                        ui.button(
                            "이 그룹 휴지통으로", color="red",
                            on_click=lambda _, name=g: do_trash_group(name),
                        ).props("flat dense")
                    with ui.row().classes("flex-wrap gap-3"):
                        for it in items:
                            _thumb_card(it)

    def select_paths(paths: list[str], on: bool):
        for p in paths:
            if p in checks:
                checks[p].value = on
        update_sel()

    def _thumb_card(it: dict):
        path = it["path"]
        with ui.card().classes("p-1").style("width:180px"):
            uri = engine.thumbnail_uri(path)
            if uri:
                ui.image(uri).classes("w-full").style("height:120px;object-fit:cover")
            else:
                ui.label("(미리보기 없음)").classes("text-xs text-gray-400")
            ui.label(Path(path).name).classes("text-xs truncate w-full").tooltip(Path(path).name)
            if it["summary"]:
                ui.label(it["summary"]).classes("text-xs text-gray-500 truncate w-full")
            checks[path] = ui.checkbox(
                "삭제 선택" + ("  🗑" if it["deletable"] else ""),
                value=False,
                on_change=lambda e: update_sel(),
            ).classes("text-xs")

    # ── 액션 ────────────────────────────────────────────────────────────────
    async def do_scan():
        root = Path(path_in.value).expanduser()
        if not root.exists():
            ui.notify(f"경로 없음: {root}", type="negative")
            return
        use_llm = engine.resolve_mode(local_sw.value)
        scan_btn.props("loading")
        scan_btn.disable()
        progress.update(i=0, total=0, running=True)
        ui.notify("스캔 시작…", type="info")
        try:
            res = await run.io_bound(
                engine.scan_images,
                root,
                use_llm=use_llm,
                with_image=img_sw.value,
                on_item=on_scan_item,
            )
        except Exception as e:
            ui.notify(f"스캔 실패: {e}", type="negative")
            return
        finally:
            progress["running"] = False
            scan_btn.props(remove="loading")
            scan_btn.enable()
        msg = f"완료: 신규 {res.new}개, 스킵 {res.skipped}개"
        if res.consolidate_error:
            msg += f" (그룹 정규화 실패: {res.consolidate_error})"
        ui.notify(msg, type="positive")
        update_stats()
        render_groups()

    async def do_trash_selected():
        paths = sorted(selected_paths())
        if not paths:
            return
        ok = await _confirm(f"{len(paths)}개를 휴지통으로 보낼까요? (복구 가능)")
        if not ok:
            return
        try:
            n = await run.io_bound(engine.trash, paths)
        except Exception as e:
            ui.notify(f"휴지통 이동 실패: {e}", type="negative")
            return
        ui.notify(f"{n}개를 휴지통으로 보냈습니다.", type="positive")
        update_stats()
        render_groups()

    async def do_trash_group(name: str):
        paths = engine.collect_paths(name, deletable=False)
        if not paths:
            return
        ok = await _confirm(f"'{name}' 그룹 {len(paths)}개를 휴지통으로 보낼까요? (복구 가능)")
        if not ok:
            return
        try:
            n = await run.io_bound(engine.trash, paths)
        except Exception as e:
            ui.notify(f"휴지통 이동 실패: {e}", type="negative")
            return
        ui.notify(f"{n}개를 휴지통으로 보냈습니다.", type="positive")
        update_stats()
        render_groups()

    async def _confirm(message: str) -> bool:
        with ui.dialog() as dialog, ui.card():
            ui.label(message)
            with ui.row().classes("justify-end w-full"):
                ui.button("취소", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button("휴지통으로", color="red", on_click=lambda: dialog.submit(True))
        return await dialog

    def on_scan_item(i, total, path, tag, error):
        progress["i"], progress["total"] = i, total  # 워커 스레드에서 호출

    def tick_progress():
        if progress["running"]:
            prog_lbl.text = f"분석 중… {progress['i']}/{progress['total']}"
        elif prog_lbl.text:
            prog_lbl.text = ""

    upd = {"status": None}

    async def check_for_update():
        st = await run.io_bound(engine.check_update)
        upd["status"] = st
        if not st.available:
            return
        if st.mode == "release":  # .app 번들 → 다운로드 페이지로 안내
            update_lbl.text = f"새 버전 {st.latest} 이 있습니다. '다운로드'로 릴리스 페이지를 엽니다."
            update_btn.text = "다운로드"
        else:                      # git 설치 → pull + 재시작
            update_lbl.text = (
                f"새 버전이 있습니다 — {st.behind}개 커밋 뒤처짐. "
                "'업데이트'를 누르면 받아서 자동 재시작합니다."
            )
            update_btn.text = "업데이트"
        update_banner.set_visibility(True)

    async def do_update():
        st = upd["status"]
        if st and st.mode == "release":  # 번들: 자체 교체 대신 다운로드 페이지 열기
            import webbrowser
            webbrowser.open(st.url or f"https://github.com/{engine.REPO_SLUG}/releases/latest")
            update_banner.set_visibility(False)
            return
        update_btn.props("loading")
        update_btn.disable()
        ok, msg = await run.io_bound(engine.apply_update)
        if not ok:
            update_btn.props(remove="loading")
            update_btn.enable()
            ui.notify(f"업데이트 실패: {msg}", type="negative")
            return
        ui.notify("업데이트 적용됨 — 재시작합니다…", type="positive")
        ui.timer(1.2, _restart, once=True)  # notify 가 렌더된 뒤 재시작

    scan_btn.on_click(do_scan)
    trash_sel_btn.on_click(do_trash_selected)
    refresh_btn.on_click(lambda: (update_stats(), render_groups()))
    update_btn.on_click(do_update)
    ui.timer(0.3, tick_progress)
    ui.timer(0.5, check_for_update, once=True)  # 로드 직후 1회 업데이트 체크

    # 최초 표시
    update_stats()
    update_sel()
    render_groups()


def _restart():
    """현재 프로세스를 같은 인자로 재실행(업데이트 적용 후 새 코드 로드)."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


def _free_port(preferred: int = 8713) -> int:
    """preferred 가 비어 있으면 그대로, 점유 중이면 OS 가 주는 빈 포트를 쓴다."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    # 개발 모드(SHOTSORT_DEV=1): 파일변경 자동 리로드. reload 는 native 와 충돌하므로
    # 이때는 브라우저로 띄운다.
    dev = os.environ.get("SHOTSORT_DEV") == "1"
    native = (os.environ.get("SHOTSORT_BROWSER") != "1") and not dev
    port = _free_port(int(os.environ.get("SHOTSORT_PORT", "8713")))
    ui.run(
        native=native,
        reload=dev,
        title="shotsort",
        window_size=(1100, 800) if native else None,
        port=port,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
