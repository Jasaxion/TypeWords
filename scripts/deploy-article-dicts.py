#!/usr/bin/env python3
"""
把 /tmp/twout 里建好的文章词库部署到 NAS。

拆成脚本而不是每次现敲命令，因为这套流程有四个容易漏的点，
漏任何一个页面上都是「词库点开是空的」或者「篇数不对」：

1. 传文件不能用 scp —— NAS 的 shell 会打一行 tmux 横幅，把 scp 协议搞乱。
   用 tar 管道。
2. 传上去的文件是 root:root 0600，容器里跑的是 uid=1000 gid=1001，读不到。
   必须 chown 1000:1001 + chmod go+rX。
3. 清单 public/list/article.json 是**构建输入**（nitro 在构建时扫 public/
   生成静态资源索引），加了条目必须重新构建镜像，重启容器不够。
   而 dicts/ 是运行时挂载，换文件不用重启。
4. length 必须等于 json 里实际的篇数 —— 过滤规则一改篇数就变，
   照抄之前打印的数字会对不上。所以这里从文件现读。

用法（先跑 check-article-dict.py 确认没问题）：
    python3 scripts/deploy-article-dicts.py \\
        --file /tmp/twout/en/article/ted-spoken.json \\
        --name "TED 日常口语" --category 口语 --tags TED
    # ...每个词库一条，最后统一重新构建
    python3 scripts/deploy-article-dicts.py --rebuild
"""

import argparse
import json
import os
import subprocess
import sys

NAS = ['ssh', '-p', '40022', '-o', 'ConnectTimeout=15', 'root@192.168.1.10']
REMOTE = '/path/to/TypeWords'


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def nas(script):
    """在 NAS 上跑一段 shell，返回 stdout（去掉那行 tmux 横幅）。"""
    r = run(NAS + [f'cd {REMOTE} && ' + script])
    return '\n'.join(l for l in r.stdout.splitlines() if 'tmux' not in l)


def upload(local, remote_name):
    """
    tar 管道传单个文件。用 -C 让归档里只有文件名，不带本地路径。
    COPYFILE_DISABLE 防止 macOS 塞 ._ 开头的 AppleDouble 文件。
    """
    env = dict(os.environ, COPYFILE_DISABLE='1')
    tar = subprocess.Popen(
        ['tar', 'czf', '-', '-C', os.path.dirname(local), os.path.basename(local)],
        stdout=subprocess.PIPE, env=env, stderr=subprocess.DEVNULL)
    dest = f'{REMOTE}/dicts/en/article'
    # 解包后立刻修好属主和权限：容器是 uid=1000 gid=1001，root:root 0600 读不到
    r = subprocess.run(
        NAS + [f'mkdir -p {dest} && tar xzf - -C {dest} && '
               f'cd {dest} && chown 1000:1001 {remote_name} && '
               f'chmod u+rw,go+r {remote_name} && ls -l {remote_name}'],
        stdin=tar.stdout, text=True, capture_output=True)
    tar.stdout.close()
    tar.wait()
    if r.returncode:
        sys.exit(f'传输失败: {r.stderr.strip()}')
    return '\n'.join(l for l in r.stdout.splitlines() if 'tmux' not in l)


def add_manifest(en_name, name, category, tags, length):
    """
    往 NAS 的 public/list/article.json 里加一条（同 enName 就覆盖）。

    在 NAS 上就地改，不是本地改完再传 —— 仓库里那份只有 1 条占位记录，
    传上去会把 NAS 上真实的清单覆盖掉。
    """
    entry = {
        'id': en_name, 'enName': en_name, 'name': name, 'description': name,
        'categoryId': 0, 'url': f'{en_name}.json', 'length': length,
        'language': 'en', 'translateLanguage': 'zh_CN', 'version': 1,
        'type': 'article', 'isDefault': False, 'recommended': False,
        'userId': None, 'cover': None, 'hidden': False,
        'category': category, 'tags': tags,
    }
    payload = json.dumps(entry, ensure_ascii=False)
    # 用 python 读写 json，不要 sed —— 清单是格式化过的多行 json
    script = f'''python3 - <<'PY'
import json
p = "public/list/article.json"
d = json.load(open(p))
e = json.loads({payload!r})
d = [x for x in d if x.get("enName") != e["enName"]] + [e]
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("清单现有", len(d), "条:", ", ".join(str(x["enName"]) for x in d))
PY'''
    return nas(script)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', help='本地的词库 json')
    ap.add_argument('--name', help='页面上显示的名字')
    ap.add_argument('--category', default='文章学习')
    ap.add_argument('--tags', nargs='+', default=['文章学习'])
    ap.add_argument('--rebuild', action='store_true',
                    help='重新构建镜像并重启（清单是构建输入，加完条目必须跑一次）')
    args = ap.parse_args()

    if args.file:
        if not args.name:
            sys.exit('--file 要配 --name')
        data = json.load(open(args.file))
        length = len(data)          # 现读，不信之前打印的数字
        en_name = os.path.splitext(os.path.basename(args.file))[0]
        print(f'{en_name}: {length} 篇')
        print(upload(args.file, f'{en_name}.json'))
        print(add_manifest(en_name, args.name, args.category, args.tags, length))
        print('清单是构建输入，全部加完后跑 --rebuild')

    if args.rebuild:
        print('构建中（几分钟，别从 NAS 的 docker 面板跑，那边会卡死）...')
        # 必须后台跑 + 重定向：ssh 会话里前台跑 compose 容易被挂断打断
        nas('nohup docker compose up -d --build > /tmp/build.log 2>&1 & echo started')
        print('已在后台开始，日志 /tmp/build.log。完成后自查：')
        print('  python3 scripts/check-live-dicts.py')


if __name__ == '__main__':
    main()
