"""Console entry points; network acquisition starts only after the service is up."""

import argparse
import logging
from pathlib import Path
import subprocess
import threading
import time
import warnings
from .config import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tighnari-seisloc", description="Tighnari-SeisLOC · 地震定位與波形監看"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--verbose", action="store_true", help="顯示逐站處理與網站存取診斷日誌")
    serve.add_argument(
        "--mode", choices=["iris", "offline", "demo", "live", "replay"], default="iris"
    )
    serve.add_argument("--file", type=Path)
    serve.add_argument("--speed", type=float, default=1)
    serve.add_argument("--travel-table", type=Path)
    setup = sub.add_parser("setup-models", help="下載固定版本 CPU PhaseNet 權重")
    setup.add_argument("--check-only", action="store_true")
    greens = sub.add_parser("cmt-build-greens", help="預算臺灣區域格林函數")
    greens.add_argument("--store", type=Path)
    greens.add_argument(
        "--diagnostic-grid", action="store_true", help="較粗網格供流程檢查，不能用於發布驗證"
    )
    greens.add_argument("--workers", default=1, type=int, choices=range(1, 17))
    greens.add_argument(
        "--short-period", action="store_true", help="建立 4–304 km、1 Hz 的短週期 CAP 波形庫"
    )
    cmt = sub.add_parser("cmt-replay", help="用獨立寬頻波形資料重播臺灣區域 CMT")
    cmt.add_argument("event", type=Path, help="Tighnari 事件 JSON")
    cmt.add_argument("output", type=Path)
    cmt.add_argument("--store", type=Path)
    cmt.add_argument(
        "--waveforms",
        nargs=2,
        action="append",
        default=[],
        metavar=("MSEED", "STATIONXML"),
        type=Path,
    )
    cmt.add_argument("--gdms-inbox", type=Path)
    cmt.add_argument("--download", action="store_true")
    methods = cmt.add_mutually_exclusive_group()
    methods.add_argument("--ensemble", action="store_true", help="以共同留出站比較多組區域 CMT")
    methods.add_argument("--moment", action="store_true", help="試算不同頻帶、震源延時與分段時移")
    methods.add_argument("--centroid", action="store_true", help="回放比較固定震央與水平矩心搜尋")
    wphase = sub.add_parser("wphase-replay", help="以 GA W-phase 獨立反演長週期矩張量")
    wphase.add_argument("event", type=Path)
    wphase.add_argument("greens", type=Path)
    wphase.add_argument("output", type=Path)
    wphase.add_argument("--python", required=True, type=Path, help="已安裝 ga-wphase 的 Python")
    sources = wphase.add_mutually_exclusive_group(required=True)
    sources.add_argument("--archive", nargs=2, type=Path, metavar=("MSEED", "STATIONXML"))
    sources.add_argument("--fdsn-server", help="由 GA W-phase 向此 FDSN 服務擷取波形")
    compare = sub.add_parser("cmt-benchmark", help="比較獨立區域 CMT 和目錄解")
    compare.add_argument("manifest", type=Path)
    compare.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    settings = Settings.read()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Outbound URLs may include a configured notification credential.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from .processing import exclusions

    exclusions.configure(settings.excluded_channels)
    if not getattr(args, "verbose", False):
        from .logging_utils import RepeatedFilter

        seedlink_logger = logging.getLogger("obspy.clients.seedlink")
        seedlink_logger.setLevel(logging.WARNING)
        seedlink_logger.addFilter(RepeatedFilter())
    if args.command == "setup-models":
        if args.check_only:
            from .processing.picking import model_for

            for name in ("original", "geofon"):
                model = model_for(name)
                print(name, model.weights_version)
        else:
            from .model_setup import setup_models

            setup_models(settings.root / "runtime/models")
    elif args.command == "cmt-build-greens":
        from .acquisition.cmt_greens import SHORT_MODELS, build_stores

        build_stores(
            args.store or settings.root / settings.regional_cmt_store,
            args.workers,
            diagnostic_grid=args.diagnostic_grid,
            **({"models": SHORT_MODELS} if args.short_period else {}),
        )
    elif args.command == "cmt-replay":
        import json

        from .acquisition.cmt import gdms_inputs
        from .acquisition.cmt_greens import GreenStores
        from .services.regional_cmt import analyze

        event = json.loads(args.event.read_text(encoding="utf-8"))
        inputs = [("local", wave, xml) for wave, xml in args.waveforms]
        if args.gdms_inbox:
            inputs.extend(gdms_inputs(args.gdms_inbox))
        greens = GreenStores(args.store or settings.root / settings.regional_cmt_store)
        result = analyze(
            event,
            greens,
            inputs,
            args.output,
            download=args.download,
            ensemble=args.ensemble
            or (not (args.moment or args.centroid) and settings.regional_cmt_ensemble),
            moment=args.moment
            or (not (args.ensemble or args.centroid) and settings.regional_cmt_moment),
            centroid=args.centroid,
            bats_url=(
                settings.regional_cmt_bats_url
                or (settings.bats_url if settings.bats_enabled else None)
            ),
        )
        print(result["status"], result["station_count"])
    elif args.command == "wphase-replay":
        from .acquisition.wphase import replay

        try:
            result = replay(
                args.event,
                args.greens,
                args.output,
                python=args.python,
                archive=args.archive,
                server=args.fdsn_server,
            )
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
        except subprocess.SubprocessError:
            parser.error(f"GA W-phase 執行失敗，請查看 {args.output / 'ga-wphase.log'}")
        print(f"Mww {result['MomentTensor']['drmag']:.2f}")
    elif args.command == "cmt-benchmark":
        from .acquisition.fdsn import save_json
        from .processing.cmt_benchmark import benchmark

        report = benchmark(args.manifest)
        save_json(args.output, report)
        print("passed" if report["passed"] else "not qualified")
    else:
        import uvicorn
        from .services.app import create_app

        if args.mode in ("demo", "replay", "live"):
            from .legacy.app import create_app as old_app
            from .legacy.travel import TableTravelTime

            warnings.warn("Legacy research mode; production uses --mode iris", FutureWarning)
            if not 0 < args.speed <= 100:
                parser.error("--speed 必須在 0 到 100 之間")
            if args.mode == "replay" and (not args.file or not args.file.is_file()):
                parser.error("回放需要有效的 --file")
            model = TableTravelTime(args.travel_table) if args.travel_table else None
            app = old_app(args.mode, args.file, args.speed, model)
        else:
            if args.travel_table:
                parser.error(
                    "新定位服務使用 iasp91；ExpTech 比較請使用舊版 iris-locate --travel-table"
                )
            app = create_app(settings, online=args.mode == "iris")
        if not args.no_browser:

            def open_browser():
                import httpx
                import webbrowser

                for _ in range(100):
                    try:
                        if httpx.get(
                            f"http://127.0.0.1:{args.port}/api/health", timeout=0.5
                        ).is_success:
                            webbrowser.open(f"http://127.0.0.1:{args.port}")
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)

            threading.Thread(target=open_browser, daemon=True).start()

        class LocalServer(uvicorn.Server):
            def handle_exit(self, sig, frame):
                # Finish long-lived SSE/WS responses before Uvicorn waits for them.
                app.state.shutting_down = True
                super().handle_exit(sig, frame)

        try:
            LocalServer(
                uvicorn.Config(
                    app,
                    host=args.host,
                    port=args.port,
                    timeout_graceful_shutdown=5,
                    access_log=args.verbose,
                )
            ).run()
        except KeyboardInterrupt:
            pass
        finally:
            if args.mode == "iris":
                from .processing.picking import close_phase_picker

                close_phase_picker()


def legacy_main():
    import sys

    warnings.warn("Use tighnari-seisloc instead of saiu-eew/saiu-locator", FutureWarning)
    if len(sys.argv) > 1 and sys.argv[1] not in ("serve", "setup-models"):
        from .legacy.cli import main as old

        old()
    else:
        main()
