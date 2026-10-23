#!/bin/sh
# Install from this reviewed checkout; no downloaded shell code is executed.
set -eu
with_wecom=false
skill_dir=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-wecom-cli) with_wecom=true ;;
        --skill-dir)
            [ "$#" -ge 2 ] || { echo '缺少 --skill-dir 目录' >&2; exit 2; }
            skill_dir=$2; shift ;;
        --help|-h)
            echo '用法: sh scripts/install.sh [--with-wecom-cli] [--skill-dir CLIENT_SKILLS_DIR]'
            echo '使用现有 uv 或 pipx 安装本检出；可选安装官方 CLI 1.3.4。Skill 已有时拒绝覆盖。'
            exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if command -v uv >/dev/null 2>&1; then
    manager=uv
elif command -v pipx >/dev/null 2>&1; then
    manager=pipx
else
    echo '请先安装 uv 或 pipx，再重新运行此入口。' >&2
    exit 1
fi
if [ "$with_wecom" = true ] && ! command -v npm >/dev/null 2>&1; then
    echo '可选企微 CLI 需要 Node.js 18+ 与 npm；也可省略 --with-wecom-cli 先安装大渊。' >&2
    exit 1
fi
if [ "$manager" = uv ]; then
    uv tool install --upgrade --reinstall "$root"
    bin_dir=$(uv tool dir --bin)
else
    pipx install --force "$root"
    bin_dir=$(pipx environment --value PIPX_BIN_DIR)
fi
if [ "$with_wecom" = true ]; then
    npm install --global @wecom/cli@1.3.4
fi
if [ -n "$skill_dir" ]; then
    "$bin_dir/dayuan" setup --skill-dir "$skill_dir"
else
    "$bin_dir/dayuan" setup
fi
"$bin_dir/dayuan" doctor
