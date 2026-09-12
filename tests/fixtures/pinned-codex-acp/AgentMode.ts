    static readonly Agent = new AgentMode(
        "agent",
        "Approve for me",
        "Only ask for actions detected as potentially unsafe",
        "auto_review",
        "on-request",
        "auto_review",
        {
            type: "workspaceWrite",
            writableRoots: [],
            networkAccess: false,
            excludeTmpdirEnvVar: false,
            excludeSlashTmp: false
        },
        "workspace-write",
    );
