#!/usr/bin/env python3
"""
自动签到脚本
"""

import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from dotenv import load_dotenv
from utils.config import AppConfig
from utils.notify import notify
from utils.balance_hash import load_balance_hash, save_balance_hash
from checkin import CheckIn

load_dotenv(override=True)

BALANCE_HASH_FILE = "balance_hash.txt"


def generate_balance_hash(balances: dict) -> str:
    """生成余额数据的hash"""
    # 将包含 quota 和 used 的结构转换为 {account_name: [quota]} 格式用于 hash 计算
    simple_balances = {}
    if balances:
        for account_key, account_balances in balances.items():
            quota_list = []
            for _, balance_info in account_balances.items():
                quota_list.append(balance_info["quota"])
            simple_balances[account_key] = quota_list

    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(balance_json.encode("utf-8")).hexdigest()[:16]


def _describe_account(account_name: str, ok_methods: list, bad_methods: list) -> str:
    """把单个账号的签到结果写成一句自然、口语的话。

    ok_methods:  [(auth_method, display_str)] 成功的登录方式及其余额描述
    bad_methods: [(auth_method, error_msg)]   失败的登录方式及原因
    display_str 形如 "Current balance: $181.11, Used: $2.0, Bonus: $0.0"
    """

    def _balance_phrase(display: str) -> str:
        # 从英文 display 里抠出余额数字，拼成中文；抠不出就原样带上
        m = re.search(r"balance:\s*\$?([\d.]+)", display, re.IGNORECASE)
        if m:
            return f"当前余额 ${m.group(1)}"
        return display.strip()

    # 全部成功
    if ok_methods and not bad_methods:
        bal = _balance_phrase(ok_methods[0][1]) if ok_methods[0][1] else ""
        tail = f"，{bal}。" if bal else "。"
        return f"「{account_name}」签到成功{tail}"

    # 全部失败
    if bad_methods and not ok_methods:
        reason = bad_methods[0][1]
        return f"「{account_name}」签到失败，原因：{reason}"

    # 部分成功部分失败
    bal = _balance_phrase(ok_methods[0][1]) if ok_methods and ok_methods[0][1] else ""
    bal_tail = f"（{bal}）" if bal else ""
    reasons = "；".join(err for _, err in bad_methods)
    return (
        f"「{account_name}」签到成功了{bal_tail}，"
        f"不过有登录方式没通过：{reasons}"
    )


async def main():
    """运行签到流程

    Returns:
            退出码: 0 表示至少有一个账号成功, 1 表示全部失败
    """

    print("🚀 newapi.ai multi-account auto check-in script started (using Camoufox)")
    print(f'🕒 Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    app_config = AppConfig.load_from_env()
    print(f"⚙️ Loaded {len(app_config.providers)} provider(s)")

    # 检查账号配置
    if not app_config.accounts:
        print("❌ Unable to load account configuration, program exits")
        return 1
    
    print(f"⚙️ Found {len(app_config.accounts)} account(s)")

    # 加载余额hash
    last_balance_hash = load_balance_hash(BALANCE_HASH_FILE)

    # 为每个账号执行签到
    success_count = 0
    total_count = 0
    accounts_total = 0
    accounts_ok = 0
    notification_content = []
    current_balances = {}
    need_notify = False  # 是否需要发送通知

    for i, account_config in enumerate(app_config.accounts):
        account_key = f"account_{i + 1}"
        account_name = account_config.get_display_name(i)
        accounts_total += 1

        try:
            provider_config = app_config.get_provider(account_config.provider)
            if not provider_config:
                print(f"❌ {account_name}: Provider '{account_config.provider}' configuration not found")
                need_notify = True
                notification_content.append(
                    f"[FAIL] {account_name}: Provider '{account_config.provider}' configuration not found"
                )
                continue

            print(f"🌀 Processing {account_name} using provider '{account_config.provider}'")
            checkin = CheckIn(account_name, account_config, provider_config, global_proxy=app_config.global_proxy)
            results = await checkin.execute()

            total_count += len(results)

            # 处理多个认证方式的结果
            account_success = False
            successful_methods = []
            failed_methods = []

            this_account_balances = {}
            # 构建自然语言结果描述
            ok_methods = []      # [(auth_method, display)]
            bad_methods = []     # [(auth_method, error_msg)]
            for auth_method, success, user_info in results:
                if success and user_info and user_info.get("success"):
                    account_success = True
                    success_count += 1
                    successful_methods.append(auth_method)
                    ok_methods.append((auth_method, user_info.get("display", "")))
                    # 记录余额信息
                    current_quota = user_info["quota"]
                    current_used = user_info["used_quota"]
                    current_bonus = user_info["bonus_quota"]
                    this_account_balances[f"{auth_method}"] = {
                        "quota": current_quota,
                        "used": current_used,
                        "bonus": current_bonus,
                    }
                else:
                    failed_methods.append(auth_method)
                    error_msg = user_info.get("error", "Unknown error") if user_info else "Unknown error"
                    bad_methods.append((auth_method, str(error_msg)))

            if account_success:
                current_balances[account_key] = this_account_balances
                accounts_ok += 1

            # 如果所有认证方式都失败，需要通知
            if not account_success and results:
                need_notify = True
                print(f"🔔 {account_name} all authentication methods failed, will send notification")

            # 如果有失败的认证方式，也通知
            if failed_methods and successful_methods:
                need_notify = True
                print(f"🔔 {account_name} has some failed authentication methods, will send notification")

            # 组装这个账号的自然语言描述
            notification_content.append(
                _describe_account(account_name, ok_methods, bad_methods)
            )

        except Exception as e:
            print(f"❌ {account_name} processing exception: {e}")
            need_notify = True  # 异常也需要通知
            notification_content.append(
                f"「{account_name}」这次没能签到，运行时报了个错：{str(e)[:100]}"
            )

    # 检查余额变化
    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    print(f"\n\nℹ️ Current balance hash: {current_balance_hash}, Last balance hash: {last_balance_hash}")
    if current_balance_hash:
        if last_balance_hash is None:
            # 首次运行
            need_notify = True
            print("🔔 First run detected, will send notification with current balances")
        elif current_balance_hash != last_balance_hash:
            # 余额有变化
            need_notify = True
            print("🔔 Balance changes detected, will send notification")
        else:
            print("ℹ️ No balance changes detected")

    # 保存当前余额hash
    if current_balance_hash:
        save_balance_hash(BALANCE_HASH_FILE, current_balance_hash)

    if need_notify and notification_content:
        # 构建自然语言通知
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        if accounts_ok == accounts_total:
            headline = f"✅ 今日签到全部完成，{accounts_total} 个站点都搞定了。"
        elif accounts_ok > 0:
            headline = f"⚠️ 今日签到部分完成：{accounts_total} 个站点里成了 {accounts_ok} 个，还有 {accounts_total - accounts_ok} 个没签上。"
        else:
            headline = f"❌ 今日签到全军覆没，{accounts_total} 个站点一个都没签上。"

        body = "\n\n".join(notification_content)
        notify_content = f"{headline}\n\n{body}\n\n（跑于 {now_str}）"

        print(notify_content)
        notify.push_message("每日签到", notify_content, msg_type="text")
        print("🔔 Notification sent due to failures or balance changes")
    else:
        print("ℹ️ All accounts successful and no balance changes detected, notification skipped")

    # 设置退出码
    sys.exit(0 if success_count > 0 else 1)


def run_main():
    """运行主函数的包装函数"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ Program interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error occurred during program execution: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_main()
