"""Skip a scheduled daily report after an earlier run sent its email."""

from datetime import datetime
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


CHINA = ZoneInfo('Asia/Shanghai')


def delivered_today(repo, token, day):
    query = urlencode({'name': f'daily-email-sent-{day}', 'per_page': 100})
    request = Request(
        f'https://api.github.com/repos/{repo}/actions/artifacts?{query}',
        headers={'Accept': 'application/vnd.github+json',
                 'Authorization': f'Bearer {token}',
                 'User-Agent': 'daily-stock-analysis'},
    )
    with urlopen(request, timeout=15) as response:
        artifacts = json.load(response)['artifacts']
    return any(not artifact['expired'] and artifact['name'] == f'daily-email-sent-{day}'
               for artifact in artifacts)


def decision(now, event, repo, token):
    local = now.astimezone(CHINA)
    day = local.date().isoformat()
    if event != 'schedule':
        return day, False, '手动运行'
    if local.hour < 15:
        return day, True, '当前不在收盘后报告时段，避免凌晨误报'
    if not repo or not token:
        raise ValueError('缺少读取Actions历史的权限，已停止定时发送以避免重复邮件')
    if delivered_today(repo, token, day):
        return day, True, '当日邮件已发送'
    return day, False, '当日尚无成功发送记录'


def main():
    day, skip, reason = decision(
        datetime.now(CHINA), os.environ.get('GITHUB_EVENT_NAME', ''),
        os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GH_TOKEN', ''),
    )
    print(f'{day}: {reason}')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'cn_date={day}\nskip={str(skip).lower()}\n')


if __name__ == '__main__':
    main()
