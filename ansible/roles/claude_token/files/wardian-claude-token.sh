# Wardian: export the Claude token only for users who can read it (the agent group).
if [ -r /etc/wardian/claude-token ]; then
	CLAUDE_CODE_OAUTH_TOKEN="$(cat /etc/wardian/claude-token)"
	export CLAUDE_CODE_OAUTH_TOKEN
fi
