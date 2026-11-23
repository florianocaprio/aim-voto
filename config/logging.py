import logging
from time import gmtime


class ServerErrorFormatter(logging.Formatter):
    """Record where a server error occurred without formatting request or exception data."""

    converter = gmtime
    local_packages = {"config", "mailings", "members", "voting"}

    def format(self, record):
        request = getattr(record, "request", None)
        route = getattr(getattr(request, "resolver_match", None), "view_name", None)
        exception_name = source = "-"
        if record.exc_info:
            exception_type, _, trace = record.exc_info
            exception_name = exception_type.__name__ if exception_type else "-"
            while trace is not None:
                module = trace.tb_frame.f_globals.get("__name__", "")
                if module.split(".", 1)[0] in self.local_packages:
                    source = f"{module}:{trace.tb_lineno}"
                trace = trace.tb_next
        stamp = self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ")
        return (
            f"{stamp} status={getattr(record, 'status_code', 500)} "
            f"view={route or '-'} error={exception_name} source={source}"
        )
