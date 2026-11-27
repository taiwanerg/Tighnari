# ruff: noqa: E402
"""GDMS status, session re-check, and verified interactive Edge login."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tighnari_seisloc.config import Settings
from tighnari_seisloc.acquisition.gdms_portal import GdmsPortal, PortalError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["resume", "login", "status"])
    parser.add_argument("--timeout", type=int, default=300, help="手動登入等待秒數（30–1800）")
    args = parser.parse_args()
    if not 30 <= args.timeout <= 1800:
        parser.error("--timeout 必須介於 30 與 1800 秒")
    settings = Settings.read()
    if args.action == "status":
        import json
        import sqlite3

        with sqlite3.connect(settings.database.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            auth = dict(db.execute("SELECT * FROM gdms_auth_state WHERE singleton=1").fetchone())
            counts = [
                dict(r)
                for r in db.execute(
                    "SELECT state,count(*) AS count FROM gdms_downloads GROUP BY state"
                )
            ]
        print(json.dumps(dict(authentication=auth, downloads=counts), ensure_ascii=False, indent=2))
        return
    if args.action == "login":
        portal = GdmsPortal(profile=settings.root / "runtime/gdms/edge-profile")
        try:
            print("請在即將開啟的專用 Edge 視窗登入 GDMS，完成帳密與驗證碼。", flush=True)
            print(f"最多等待 {args.timeout} 秒；驗證成功後視窗將自動關閉。", flush=True)
            portal.manual_login(settings.root / "runtime/gdms/manual-session", args.timeout)
        except ImportError:
            parser.exit(1, '請先安裝瀏覽器支援：python -m pip install -e ".[gdms-edge]"\n')
        except PortalError as exc:
            parser.exit(1, f"手動登入未完成：{exc}；未要求恢復佇列。\n")
        finally:
            portal.close()
    if args.action in {"resume", "login"}:
        if args.action == "resume":
            marker = settings.root / "runtime/gdms/resume-auth"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        print("GDMS 將在下次輪詢接收或重新檢查登入狀態，並恢復可重試工作。")
        if args.action == "login":
            print("請保持背景服務運行；登入交接檔限同一系統使用者使用，10 分鐘後失效。")


if __name__ == "__main__":
    main()
