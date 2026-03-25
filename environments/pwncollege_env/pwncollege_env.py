"""
PwnCollege Training Environment for Hermes-Agent + Atropos

Uses hermes-agent's tool system and HermesAgentLoop for the agent,
with pwn.college SDK + SSH for challenge container management.

Usage:
    python environments/pwncollege_env/pwncollege_env.py serve \
        --config environments/pwncollege_env/default.yaml

    python environments/pwncollege_env/pwncollege_env.py process \
        --config environments/pwncollege_env/default.yaml \
        --env.data_path_to_save_groups sft_data.jsonl

    python environments/pwncollege_env/pwncollege_env.py evaluate \
        --config environments/pwncollege_env/default.yaml
"""

import atexit
import json
import logging
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import Field

# Ensure repo root is on sys.path
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from dotenv import load_dotenv

_env_path = _repo_root / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)

from environments.patches import apply_patches

apply_patches()

from atroposlib.envs.base import APIServerConfig, ScoredDataItem
from atroposlib.type_definitions import Item

from environments.agent_loop import AgentResult, HermesAgentLoop
from environments.hermes_base_env import HermesAgentBaseEnv, HermesAgentEnvConfig

# Import submit_flag_tool to trigger registry.register() at module load
from environments.pwncollege_env import submit_flag_tool  # noqa: F401
from environments.pwncollege_env.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from environments.pwncollege_env.sdk import DojoRLClient, DojoRLSyncClient, RLChallenge
from environments.pwncollege_env.submit_flag_tool import (
    clear_flag_context,
    register_flag_context,
)
from environments.tool_context import ToolContext
from tools.terminal_tool import (
    cleanup_vm,
    clear_task_env_overrides,
    register_task_env_overrides,
)

logger = logging.getLogger(__name__)


class PwnCollegeEnvConfig(HermesAgentEnvConfig):
    """Configuration for PwnCollege environment."""

    # Dojo connection
    base_url: str = Field(
        default="http://100.120.55.25:8080",
        description="Dojo API base URL",
    )
    ssh_host: str = Field(
        default="100.120.55.25",
        description="SSH host for challenge containers",
    )
    ssh_port: int = Field(default=2222, description="SSH port")
    ssh_key: str = Field(
        default="",
        description="Path to SSH private key for RL agent",
    )

    # Challenge selection
    challenge: str = Field(
        default="hello/hello",
        description="Challenge in module/challenge format (e.g., 'hello/hello', 'paths/root')",
    )
    dojo_filter: Optional[str] = Field(default=None, description="Filter by dojo ID")
    module_filter: Optional[str] = Field(
        default=None, description="Filter by module ID"
    )

    # Eval settings
    eval_dojo: Optional[str] = Field(
        default=None,
        description="Dojo to evaluate on (None = all dojos)",
    )
    eval_exclude_dojos: List[str] = Field(
        default_factory=list,
        description="Dojos to exclude from evaluation",
    )
    eval_module: Optional[str] = Field(
        default=None,
        description="Module to evaluate on (None = all modules)",
    )
    eval_concurrency: int = Field(
        default=4,
        description="Max concurrent eval episodes (limited by dojo slots)",
    )


class PwnCollegeEnv(HermesAgentBaseEnv):
    """PwnCollege training environment.

    Lifecycle per rollout:
    1. Create dojo instance (SDK) → get slot + ssh_user
    2. Register SSH overrides so terminal tool routes to that instance
    3. Register flag context so submit_flag tool can verify flags
    4. Run hermes-agent loop (terminal + file + submit_flag tools)
    5. Score: did agent submit the correct flag?
    6. Cleanup: destroy instance, clear overrides
    """

    name = "pwncollege"
    env_config_cls = PwnCollegeEnvConfig

    def __init__(
        self,
        config: PwnCollegeEnvConfig,
        server_configs: List[APIServerConfig],
        slurm: bool = False,
        testing: bool = False,
    ):
        # Set global SSH env vars before super().__init__ triggers terminal validation.
        # Per-task overrides (ssh_user) are registered before each rollout.
        os.environ.setdefault("TERMINAL_SSH_HOST", config.ssh_host)
        os.environ.setdefault("TERMINAL_SSH_USER", "rl_0")
        os.environ.setdefault("TERMINAL_SSH_KEY", config.ssh_key)

        # Patch api_key from env var before super().__init__ bakes it into openai.AsyncClient
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if api_key:
            for sc in server_configs:
                if not sc.api_key:
                    sc.api_key = api_key

        super().__init__(config, server_configs, slurm, testing)
        self.config: PwnCollegeEnvConfig = config

        self.train: list[RLChallenge] = []
        self.iter = 0
        self.solve_rate_buffer: list[float] = []
        self._active_slots: set[int] = set()

        # SDK clients — async for setup/lifecycle, sync for submit_flag handler
        self.client: Optional[DojoRLClient] = None
        self.sync_client: Optional[DojoRLSyncClient] = None

    @classmethod
    def config_init(cls) -> Tuple[PwnCollegeEnvConfig, List[APIServerConfig]]:
        env_config = PwnCollegeEnvConfig(
            enabled_toolsets=["terminal", "file", "pwncollege"],
            max_agent_turns=20,
            max_token_length=16384,
            agent_temperature=0.7,
            terminal_backend="ssh",
            system_prompt=SYSTEM_PROMPT,
            use_wandb=True,
            wandb_name="pwncollege",
            ensure_scores_are_not_same=False,
        )
        server_configs = [
            APIServerConfig(
                base_url="https://openrouter.ai/api/v1",
                model_name="anthropic/claude-sonnet-4.5",
                server_type="openai",
                api_key=os.getenv("OPENROUTER_API_KEY", ""),
                health_check=False,
            ),
        ]
        return env_config, server_configs

    def _cleanup_instances(self):
        """Destroy all running dojo instances. Called on exit/signal."""
        if not self.sync_client:
            return
        try:
            n = self.sync_client.destroy_all()
            if n:
                logger.info("Cleaned up %d dojo instance(s)", n)
        except Exception as e:
            logger.warning("Instance cleanup failed: %s", e)

    def _signal_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM: clean up instances, then re-raise."""
        logger.info("Signal %d received, cleaning up dojo instances...", signum)
        self._cleanup_instances()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    async def setup(self):
        """Load challenges from dojo and initialize SDK clients."""
        self.client = DojoRLClient(self.config.base_url)
        self.sync_client = DojoRLSyncClient(self.config.base_url)

        atexit.register(self._cleanup_instances)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Fetch challenges
        challenges = await self.client.list_challenges()
        logger.info("Fetched %d challenges from dojo", len(challenges))

        # Apply filters
        for c in challenges:
            if self.config.dojo_filter and c.dojo_id != self.config.dojo_filter:
                continue
            if self.config.module_filter and c.module_id != self.config.module_filter:
                continue
            self.train.append(c)

        # If a specific challenge is set and no filters matched, use it directly
        if not self.train and self.config.challenge:
            parts = self.config.challenge.split("/")
            self.train.append(
                RLChallenge(
                    id=parts[-1],
                    module_id=parts[0],
                    dojo_id="unknown",
                    name=self.config.challenge,
                    description="",
                )
            )

        if not self.train:
            raise RuntimeError(
                f"No challenges matched filters (dojo_filter={self.config.dojo_filter}, "
                f"module_filter={self.config.module_filter}, challenge={self.config.challenge}). "
                f"Total available: {len(challenges)}"
            )

        logger.info("Training on %d challenges", len(self.train))

    async def get_next_item(self) -> RLChallenge:
        """Return next challenge item (round-robin)."""
        item = self.train[self.iter % len(self.train)]
        self.iter += 1
        return item

    def _get_challenge_key(self, item: RLChallenge) -> str:
        """Extract the challenge key from a challenge."""
        return item.challenge_key or f"{item.module_id or ''}/{item.id}"

    def format_prompt(self, item: RLChallenge) -> str:
        """Build user prompt from challenge metadata."""
        challenge_key = self._get_challenge_key(item)
        return USER_PROMPT_TEMPLATE.format(
            module_name=item.module_id or "unknown",
            challenge_name=item.name or item.id,
            challenge_description=item.description or f"Solve the challenge: {challenge_key}",
        )

    async def collect_trajectory(
        self, item: Item
    ) -> Tuple[Optional[Union[ScoredDataItem, Any]], List[Item]]:
        """Run a single rollout with dojo instance lifecycle.

        Wraps the agent loop with:
        1. Dojo instance creation (SSH-accessible challenge container)
        2. SSH override registration (routes terminal tool to the instance)
        3. Flag context registration (enables submit_flag tool)
        4. Cleanup on completion
        """
        task_id = str(uuid.uuid4())
        challenge_key = self._get_challenge_key(item)

        try:
            inst = await self.client.create_instance(challenge_key)
        except Exception as e:
            logger.error("Failed to create instance for %s: %s", challenge_key, e)
            return None, []

        slot = inst.slot
        self._active_slots.add(slot)
        register_task_env_overrides(
            task_id,
            {
                "ssh_user": inst.ssh_user,
                "ssh_host": self.config.ssh_host,
                "ssh_port": self.config.ssh_port,
                "ssh_key": self.config.ssh_key,
            },
        )
        register_flag_context(task_id, self.sync_client, slot)

        try:
            # Resolve tools (includes submit_flag via "pwncollege" toolset)
            if self._current_group_tools is None:
                tools, valid_names = self._resolve_tools_for_group()
            else:
                tools, valid_names = self._current_group_tools

            messages: List[Dict[str, Any]] = []
            if self.config.system_prompt:
                messages.append({"role": "system", "content": self.config.system_prompt})
            messages.append({"role": "user", "content": self.format_prompt(item)})

            agent = HermesAgentLoop(
                server=self.server,
                tool_schemas=tools,
                valid_tool_names=valid_names,
                max_turns=self.config.max_agent_turns,
                task_id=task_id,
                temperature=self.config.agent_temperature,
                max_tokens=self.config.max_token_length,
                extra_body=self.config.extra_body,
            )
            result = await agent.run(messages)

            # Skip reward if agent produced no output
            only_system_and_user = all(
                msg.get("role") in ("system", "user") for msg in result.messages
            )
            if result.turns_used == 0 or only_system_and_user:
                logger.warning("Agent produced no output for %s", challenge_key)
                reward = 0.0
            else:
                ctx = ToolContext(task_id)
                try:
                    reward = await self.compute_reward(item, result, ctx)
                finally:
                    ctx.cleanup()

            # Track tool errors
            if result.tool_errors:
                for err in result.tool_errors:
                    self._tool_error_buffer.append({
                        "turn": err.turn,
                        "tool": err.tool_name,
                        "args": err.arguments[:150],
                        "error": err.error[:300],
                        "result": err.tool_result[:300],
                    })

            # Build scored item (Phase 1: placeholder tokens)
            full_text = "\n".join(
                msg.get("content", "") for msg in result.messages if msg.get("content")
            )
            if self.tokenizer:
                tokens = self.tokenizer.encode(full_text, add_special_tokens=True)
            else:
                tokens = list(range(min(len(full_text) // 4, 128)))

            scored_item = {
                "tokens": tokens,
                "masks": [-100] + tokens[1:],
                "scores": reward,
                "messages": result.messages,
            }
            return scored_item, []

        finally:
            clear_flag_context(task_id)
            clear_task_env_overrides(task_id)
            cleanup_vm(task_id)
            try:
                await self.client.destroy_instance(slot)
            except Exception as e:
                logger.warning("Failed to destroy instance slot %d: %s", slot, e)
            self._active_slots.discard(slot)

    async def compute_reward(
        self, item: Item, result: AgentResult, ctx: ToolContext
    ) -> float:
        """Score the rollout: 1.0 if flag was correctly submitted, 0.0 otherwise.

        Checks two signals:
        1. Did submit_flag return {"success": true}?
        2. Fallback: extract pwn.college{...} from terminal output and verify via SDK.
        """
        # Check submit_flag tool results in the conversation
        for msg in result.messages:
            if msg.get("role") == "tool":
                try:
                    data = json.loads(msg.get("content", ""))
                    if isinstance(data, dict) and data.get("success") is True:
                        self.solve_rate_buffer.append(1.0)
                        return 1.0
                except (json.JSONDecodeError, TypeError):
                    pass

        # Fallback: scan for flag pattern in all messages
        for msg in result.messages:
            content = msg.get("content", "")
            if not content:
                continue
            flag_match = re.search(r"pwn\.college\{[^}]+\}", content)
            if flag_match:
                # We can't verify here since instance is being torn down,
                # but the flag pattern presence suggests partial progress
                self.solve_rate_buffer.append(0.0)
                return 0.0

        self.solve_rate_buffer.append(0.0)
        return 0.0

    async def evaluate(self, *args, **kwargs):
        """Run evaluation on a dojo/module and report solve rate.

        Fetches challenges matching eval_dojo/eval_module, runs each through
        the agent loop with concurrency control, and logs results.
        """
        import asyncio
        import time

        if not self.client:
            logger.error("SDK client not initialized. Call setup() first.")
            return

        start_time = time.time()

        # Fetch and filter eval challenges
        all_challenges = await self.client.list_challenges()
        eval_challenges = [
            c for c in all_challenges
            if (self.config.eval_dojo is None or c.dojo_id == self.config.eval_dojo)
            and (self.config.eval_module is None or c.module_id == self.config.eval_module)
            and c.dojo_id not in self.config.eval_exclude_dojos
        ]

        if not eval_challenges:
            logger.warning(
                "No challenges found for eval_dojo=%s eval_module=%s",
                self.config.eval_dojo, self.config.eval_module,
            )
            return

        print(
            f"Evaluating {len(eval_challenges)} challenges from "
            f"{self.config.eval_dojo or '*'}/{self.config.eval_module or '*'} "
            f"(concurrency={self.config.eval_concurrency})",
            flush=True,
        )

        semaphore = asyncio.Semaphore(self.config.eval_concurrency)
        completed = 0
        total = len(eval_challenges)

        async def eval_one(challenge: RLChallenge) -> dict:
            nonlocal completed
            challenge_key = self._get_challenge_key(challenge)
            async with semaphore:
                try:
                    scored, _ = await self.collect_trajectory(challenge)
                    solved = scored is not None and scored.get("scores", 0.0) >= 1.0
                    completed += 1
                    status = "PASS" if solved else "FAIL"
                    reward = scored.get("scores", 0.0) if scored else 0.0
                    print(
                        f"  [{completed}/{total}] [{status}] {challenge_key} "
                        f"(reward={reward:.1f})",
                        flush=True,
                    )
                    return {
                        "challenge": challenge_key,
                        "name": challenge.name,
                        "solved": solved,
                        "reward": reward,
                    }
                except Exception as e:
                    completed += 1
                    print(
                        f"  [{completed}/{total}] [ERR ] {challenge_key}: {e}",
                        flush=True,
                    )
                    return {
                        "challenge": challenge_key,
                        "name": challenge.name,
                        "solved": False,
                        "reward": 0.0,
                        "error": str(e),
                    }

        tasks = [eval_one(c) for c in eval_challenges]
        results = await asyncio.gather(*tasks)

        end_time = time.time()

        # Aggregate
        n = len(results)
        solved = sum(1 for r in results if r["solved"])
        solve_rate = solved / n if n else 0.0

        print("=" * 60, flush=True)
        print(
            f"Eval: {solved}/{n} solved ({solve_rate * 100:.1f}%) "
            f"in {end_time - start_time:.1f}s",
            flush=True,
        )
        print("=" * 60, flush=True)

        eval_metrics = {
            "eval/solve_rate": solve_rate,
            "eval/solved": solved,
            "eval/total": n,
        }

        samples = [
            {
                "prompt": r["challenge"],
                "response": "SOLVED" if r["solved"] else "FAILED",
                "expected": "SOLVED",
                "reward": r["reward"],
            }
            for r in results
        ]

        await self.evaluate_log(
            metrics=eval_metrics,
            samples=samples,
            start_time=start_time,
            end_time=end_time,
        )

    async def wandb_log(self, wandb_metrics: Optional[Dict] = None):
        """Log solve rate metrics to wandb."""
        if wandb_metrics is None:
            wandb_metrics = {}
        if self.solve_rate_buffer:
            n = len(self.solve_rate_buffer)
            wandb_metrics["train/solve_rate"] = sum(self.solve_rate_buffer) / n
            wandb_metrics["train/num_rollouts"] = n
            self.solve_rate_buffer = []
        await super().wandb_log(wandb_metrics)


if __name__ == "__main__":
    PwnCollegeEnv.cli()
