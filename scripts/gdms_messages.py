"""Chinese GDMS console messages, kept outside CMD's batch parser."""

import sys

MESSAGES = {
    "prompt": "請選擇操作：",
    "missing_control": "缺少 scripts\\gdms_control.py。請先同步完整程式更新。",
    "old_control": "遠端仍是舊版 GDMS 指令。請先同步 Mac 的完整修正，再重啟 Tighnari。",
    "sync_required": "只複製這個 bat 檔無法更新登入與恢復邏輯。",
    "same_user": "請以與 Tighnari 服務相同的 Windows 帳號操作。",
    "login_instructions": "在即將開啟的 Edge 視窗完成帳密與驗證碼登入，最多等待十分鐘。",
    "login_verified": "登入已驗證；背景服務將於下次輪詢接收登入狀態。",
    "service_unreachable_status": "注意：8765 服務目前未回應；以上是資料庫狀態。",
    "install_edge": "將在目前啟用的 conda 環境安裝專案的 Edge 支援。",
    "edge_installed": "安裝完成。電腦也需要安裝 Microsoft Edge。",
    "service_running": "8765 服務已在運行，不另開第二份服務。",
    "service_started": "已開啟服務視窗。待啟動完成後，再選擇登入或查看狀態。",
    "service_missing": "8765 服務目前未回應。請先選擇 5 啟動服務，完成後再登入或恢復。",
    "action_failed": "操作未完成，請查看上方訊息；未宣告登入或恢復成功。",
}

MENU = [
    "1. 手動登入 GDMS（開啟專用 Edge 視窗）",
    "2. 重新檢查登入並恢復下載",
    "3. 查看登入與下載狀態",
    "4. 安裝或補齊 Edge 登入支援（首次使用）",
    "5. 啟動 Tighnari 服務（另開視窗）",
    "0. 結束",
]


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {*MESSAGES, "menu"}:
        raise SystemExit("Usage: gdms_messages.py <message key|menu>")
    key = sys.argv[1]
    print("\n".join(MENU) if key == "menu" else MESSAGES[key])


if __name__ == "__main__":
    main()
