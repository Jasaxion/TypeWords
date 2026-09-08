#!/usr/bin/env python3
"""
把建好的词库（文章或单词）部署到 NAS。

拆成脚本而不是每次现敲命令，因为这套流程有四个容易漏的点，
漏任何一个页面上都是「词库点开是空的」或者「篇数不对」：

1. 传文件不能用 scp —— 远端 shell 只要打了横幅（tmux 之类）就会把 scp
   协议搞乱，表现是 exit 0 但内容是旧的。用 tar 管道。
2. 传上去的文件是 root:root 0600，容器里跑的不是 root，读不到。
   必须 chown 成容器的 uid:gid（`TW_OWNER`）+ chmod go+rX。
3. 清单 public/list/{article,word}.json 是**构建输入**（nitro 在构建时扫
   public/ 生成静态资源索引），加了条目必须重新构建镜像，重启容器不够。
   而 dicts/ 是运行时挂载，换文件不用重启。
4. length 必须等于 json 里实际的条数 —— 过滤规则一改条数就变，
   照抄之前打印的数字会对不上。所以这里从文件现读。

用法（先跑 check-article-dict.py 确认没问题）：
    export TW_SSH="-p 40022 root@192.168.1.10"   # 你的部署机
    export TW_REMOTE=/path/to/TypeWords
    export TW_OWNER=1000:1001                    # 容器的 uid:gid，见下

    python3 scripts/deploy-article-dicts.py \\
        --file /tmp/twout/en/article/ted-spoken.json \\
        --name "TED 日常口语" --category 口语 --tags TED
    # 单词词库加 --kind word
    python3 scripts/deploy-article-dicts.py --kind word \\
        --file /tmp/wd/ai-ml.json --name "AI 与机器学习" \\
        --category 专业领域 --tags 人工智能
    # ...每个词库一条，最后统一重新构建
    python3 scripts/deploy-article-dicts.py --rebuild

    # 朗读音频（synth-article-audio.py 的产出）。运行时挂载，不用 --rebuild，
    # 但改过的词库 json 要一起传，因为 audioSrc/lrcPosition 写在里面
    python3 scripts/deploy-article-dicts.py --audio-dir /tmp/twaudio/ted-spoken

**重建已在学的词库前先看进度**：lastLearnIndex 是位置下标，词序一变
进度就指向别的词。查 data/typing-word-dict 里对应 enName 是不是 0。
"""

import argparse
import json
import os
import subprocess
import sys

# 部署目标从环境变量读，不写死在仓库里。
#   TW_SSH     ssh 目标，形如 "-p 40022 root@192.168.1.10" 或就一个 "myhost"
#              （配好 ~/.ssh/config 的话）
#   TW_REMOTE  远端仓库路径
#   TW_OWNER   容器里跑的 uid:gid。**必须和容器一致**，否则文件属主不对、
#              容器读不到，页面上表现为「词库点开是空的」。
#              查法：docker exec <容器> id
SSH_ARGS = os.environ.get('TW_SSH', '').split()
REMOTE = os.environ.get('TW_REMOTE', '')
OWNER = os.environ.get('TW_OWNER', '1000:1000')

if not SSH_ARGS or not REMOTE:
    sys.exit('请先设置 TW_SSH 和 TW_REMOTE，例如：\n'
             '  export TW_SSH="-p 40022 root@192.168.1.10"\n'
             '  export TW_REMOTE=/path/to/TypeWords\n'
             '  export TW_OWNER=1000:1001   # 可选，默认 1000:1000')

NAS = ['ssh', '-o', 'ConnectTimeout=15'] + SSH_ARGS


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def nas(script):
    """在 NAS 上跑一段 shell，返回 stdout（去掉那行 tmux 横幅）。"""
    r = run(NAS + [f'cd {REMOTE} && ' + script])
    return '\n'.join(l for l in r.stdout.splitlines() if 'tmux' not in l)


def upload(local, remote_name, kind='article'):
    """
    tar 管道传单个文件。用 -C 让归档里只有文件名，不带本地路径。
    COPYFILE_DISABLE 防止 macOS 塞 ._ 开头的 AppleDouble 文件。
    """
    env = dict(os.environ, COPYFILE_DISABLE='1')
    tar = subprocess.Popen(
        ['tar', 'czf', '-', '-C', os.path.dirname(local), os.path.basename(local)],
        stdout=subprocess.PIPE, env=env, stderr=subprocess.DEVNULL)
    dest = f'{REMOTE}/dicts/en/{kind}'
    # 解包后立刻修好属主和权限：容器不是 root，root:root 0600 读不到
    r = subprocess.run(
        NAS + [f'mkdir -p {dest} && tar xzf - -C {dest} && '
               f'cd {dest} && chown {OWNER} {remote_name} && '
               f'chmod u+rw,go+r {remote_name} && ls -l {remote_name}'],
        stdin=tar.stdout, text=True, capture_output=True)
    tar.stdout.close()
    tar.wait()
    if r.returncode:
        sys.exit(f'传输失败: {r.stderr.strip()}')
    return '\n'.join(l for l in r.stdout.splitlines() if 'tmux' not in l)


def upload_audio(local_dir, en_name):
    """
    传一个文章库的朗读音频目录（synth-article-audio.py 的产出）。

    和词库 json 一样走 tar 管道、传完就 chown —— 音频是运行时挂载的
    （server/routes/audio/[...path].get.ts），不用重新构建镜像。

    不要把音频放进 public/sound/：那是构建输入，进了镜像每加一篇文章
    都要重新构建，而且运行时替换同名文件会被按旧 size 截断。
    """
    if not os.path.isdir(local_dir):
        sys.exit(f'音频目录不存在: {local_dir}')
    # 后缀跟着 server/routes/audio 的白名单走。默认编码是 opus(.ogg)，
    # 只认 .mp3 的话换了编码就会报「里没有 mp3」——实际是有音频的。
    exts = ('.ogg', '.mp3', '.m4a', '.wav')
    files = sorted(f for f in os.listdir(local_dir) if f.endswith(exts))
    if not files:
        sys.exit(f'{local_dir} 里没有音频（{"/".join(exts)}）')
    total = sum(os.path.getsize(os.path.join(local_dir, f)) for f in files)
    print(f'音频 {len(files)} 个文件 {total / 1024 / 1024:.0f} MB -> {en_name}/')

    env = dict(os.environ, COPYFILE_DISABLE='1')
    tar = subprocess.Popen(
        ['tar', 'czf', '-', '-C', os.path.dirname(os.path.abspath(local_dir)),
         os.path.basename(os.path.abspath(local_dir))],
        stdout=subprocess.PIPE, env=env, stderr=subprocess.DEVNULL)
    dest = f'{REMOTE}/audio'
    r = subprocess.run(
        NAS + [f'mkdir -p {dest} && tar xzf - -C {dest} && '
               f'chown -R {OWNER} {dest}/{en_name} && '
               f'chmod -R u+rw,go+rX {dest}/{en_name} && '
               f'du -sh {dest}/{en_name} && ls {dest}/{en_name} | wc -l'],
        stdin=tar.stdout, text=True, capture_output=True)
    tar.stdout.close()
    tar.wait()
    if r.returncode:
        sys.exit(f'音频传输失败: {r.stderr.strip()}')
    return '\n'.join(l for l in r.stdout.splitlines() if 'tmux' not in l)



def add_manifest(en_name, name, category, tags, length, kind='article'):
    """
    往 NAS 的 public/list/{kind}.json 里加一条（同 enName 就覆盖）。

    在 NAS 上就地改，不是本地改完再传 —— 仓库里那份只有 1 条占位记录，
    传上去会把 NAS 上真实的清单覆盖掉。

    已经在清单里的词库（比如重建 ai-ml）走同一条路径：按 enName 覆盖，
    顺便把 length 更新成新的条数。
    """
    entry = {
        'id': en_name, 'enName': en_name, 'name': name, 'description': name,
        'categoryId': 0, 'url': f'{en_name}.json', 'length': length,
        'language': 'en', 'translateLanguage': 'zh_CN', 'version': 1,
        'type': kind, 'isDefault': False, 'recommended': False,
        'userId': None, 'cover': None, 'hidden': False,
        'category': category, 'tags': tags,
    }
    payload = json.dumps(entry, ensure_ascii=False)
    # 用 python 读写 json，不要 sed —— 清单是格式化过的多行 json
    script = f'''python3 - <<'PY'
import json
p = "public/list/{kind}.json"
d = json.load(open(p))
e = json.loads({payload!r})
d = [x for x in d if x.get("enName") != e["enName"]] + [e]
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("清单现有", len(d), "条")
PY'''
    return nas(script)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', help='本地的词库 json')
    ap.add_argument('--name', help='页面上显示的名字')
    ap.add_argument('--kind', choices=['article', 'word'], default='article',
                    help='文章词库还是单词词库。决定传到 dicts/en/<kind>/ '
                         '和写进 public/list/<kind>.json')
    ap.add_argument('--category', default='文章学习')
    ap.add_argument('--tags', nargs='+', default=['文章学习'])
    ap.add_argument('--rebuild', action='store_true',
                    help='重新构建镜像并重启（清单是构建输入，加完条目必须跑一次）')
    ap.add_argument('--audio-dir',
                    help='朗读音频目录（synth-article-audio.py 的 <out>/<enName>）。'
                         '音频是运行时挂载，传完不用 --rebuild')
    args = ap.parse_args()

    if args.audio_dir:
        en_name = os.path.basename(os.path.abspath(args.audio_dir))
        print(upload_audio(args.audio_dir, en_name))
        print('音频是运行时挂载，不用 --rebuild。'
              '但词库 json 里的 audioSrc/lrcPosition 也要一起传（--file）')

    if args.file:
        if not args.name:
            sys.exit('--file 要配 --name')
        data = json.load(open(args.file))
        length = len(data)          # 现读，不信之前打印的数字
        en_name = os.path.splitext(os.path.basename(args.file))[0]
        print(f'{en_name}: {length} {"篇" if args.kind == "article" else "个词"}')
        print(upload(args.file, f'{en_name}.json', args.kind))
        print(add_manifest(en_name, args.name, args.category, args.tags,
                           length, args.kind))
        print('清单是构建输入，全部加完后跑 --rebuild')

    if args.rebuild:
        print('构建中（几分钟，别从 NAS 的 docker 面板跑，那边会卡死）...')
        # 必须后台跑 + 重定向：ssh 会话里前台跑 compose 容易被挂断打断
        nas('nohup docker compose up -d --build > /tmp/build.log 2>&1 & echo started')
        print('已在后台开始，日志 /tmp/build.log。完成后自查：')
        print('  python3 scripts/check-live-dicts.py')


if __name__ == '__main__':
    main()
