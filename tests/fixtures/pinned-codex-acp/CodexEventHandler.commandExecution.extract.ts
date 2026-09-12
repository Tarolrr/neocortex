    private completeCommandExecutionEvent(item: ThreadItem & { "type": "commandExecution" }): UpdateSessionEvent {
        const update: UpdateSessionEvent = {
            sessionUpdate: "tool_call_update",
            toolCallId: item.id,
            status: item.status === "completed" ? "completed" : "failed",
            rawOutput: {
                formatted_output: item.aggregatedOutput ?? "",
                exit_code: item.exitCode
            },
        };
