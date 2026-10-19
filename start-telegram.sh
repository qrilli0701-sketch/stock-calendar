#!/bin/zsh
# 텔레그램으로 Claude Code와 대화하기. 이 창이 열려 있는 동안만 답장이 온다.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
# 봇 토큰은 프로젝트 .env에서 읽는다 (화면에 출력하지 않음)
export TELEGRAM_BOT_TOKEN="$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)"
# caffeinate: 이 창이 실행 중인 동안 Mac이 잠들지 않게 (덮개를 닫으면 배터리 상태에선 잠듦)
exec caffeinate -i claude --channels plugin:telegram@claude-plugins-official
