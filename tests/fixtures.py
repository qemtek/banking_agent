from harness import Reply, ToolCall


class BalanceScript:
    """Fixed interaction for the balance task, independent of user wording."""
    def respond(self, messages, tools):
        last = messages[-1]
        if last["role"] == "tool":
            value = last["value"]
            return Reply(f"Your available balance is {value['currency']} {value['amount']}.")
        return ToolCall("get_available_balance", {})
