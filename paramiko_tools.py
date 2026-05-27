from __future__ import annotations

from dataclasses import dataclass

import paramiko
from langchain_core.tools import tool

@tool
def connect_ssh(host: str, username: str, password: str, port: int = 22, timeout: int = 10, key_filename: str | None = None) -> str:
    """connect to host using paramiko ssh.connect."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            key_filename=key_filename,
        )
        return f"Connected to {host}:{port}"
    except Exception as error:
        return f"Error: {error}"
    finally:
        client.close()


@tool
def get_process(host: str, username: str, password: str, port: int = 22, timeout: int = 10, key_filename: str | None = None) -> str:
    """get_process: run the GoldenGate 'info all' command."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
        )
        _, stdout, stderr = client.exec_command("info all")
        output = stdout.read().decode("utf-8", errors="replace")
        error_output = stderr.read().decode("utf-8", errors="replace")
        return output if output else error_output
    except Exception as error:
        return f"Error: {error}"
    finally:
        client.close()


@tool
def check_log(host: str, username: str, password: str, process_name: str, port: int = 22, timeout: int = 10) -> str:
    """Check log: run 'view report <process_name>' for the selected process."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
        )
        _, stdout, stderr = client.exec_command(f"view report {process_name}")
        output = stdout.read().decode("utf-8", errors="replace")
        error_output = stderr.read().decode("utf-8", errors="replace")
        return output if output else error_output
    except Exception as error:
        return f"Error: {error}"
    finally:
        client.close()


@tool
def check_disk(host: str, username: str, password: str, port: int = 22, timeout: int = 10) -> str:
    """Check disk: run 'df -h' to inspect filesystem usage."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
        )
        _, stdout, stderr = client.exec_command("df -h")
        output = stdout.read().decode("utf-8", errors="replace")
        error_output = stderr.read().decode("utf-8", errors="replace")
        return output if output else error_output
    except Exception as error:
        return f"Error: {error}"
    finally:
        client.close()
