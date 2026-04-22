"""Run the AWS DevOps AI master agent interactively or with a single query."""

import logging
import os
import sys

from aws_devops_ai.models import LogSource, LogSourceType, SystemConfig
from aws_devops_ai.master_agent import MasterAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def _print_credits(agent):
    """Print credit usage summary from all KiroSessions used during the run."""
    tracker = agent.modules.credit_tracker
    print(f"\n{tracker.format_report()}")


def main():
    source_dir = sys.argv[1] if len(sys.argv) > 1 else "test_logs"
    base_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    query = sys.argv[3] if len(sys.argv) > 3 else None

    topo_dir = os.path.join(base_dir, "topology_output")
    rca_dir = os.path.join(base_dir, "rca_output")

    config = SystemConfig(
        topology_path=f"{topo_dir}/topology.json",
        topology_audit_log_path=f"{topo_dir}/topology_audit.jsonl",
        topology_output_dir=topo_dir,
        rca_output_dir=rca_dir,
        resource_map_path=f"{topo_dir}/resource_map.json",
        tracker_db="",
        log_dir=source_dir,
        sources=[LogSource(LogSourceType.LOCAL_FILE, source_dir)],
    )

    agent = MasterAgent(config)

    if query:
        # Single query mode
        greeting = agent.start()
        print(f"Agent: {greeting}\n")
        response = agent.send(query)
        print(f"Agent: {response}")
        _print_credits(agent)
        agent.end()
    else:
        # Interactive mode
        greeting = agent.start()
        print(f"Agent: {greeting}\n")
        try:
            while True:
                user_input = input("You: ").strip()
                if not user_input or user_input.lower() in ("exit", "quit", "bye"):
                    break
                response = agent.send(user_input)
                print(f"\nAgent: {response}\n")
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            _print_credits(agent)
            agent.end()
            print("\nSession ended.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
