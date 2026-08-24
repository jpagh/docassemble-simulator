"""Session engine: load an interview, run its flow, answer screens, persist state.

This mirrors what the docassemble server does on every request:

1. ``interview.assemble(user_dict, interview_status)`` runs the mandatory
   code blocks. When they reference an undefined variable, askfor resolves
   it (code blocks, objects blocks, data blocks...) and either continues or
   stops at a question screen, which lands in ``interview_status``.
2. The agent reads the screen's fields, sets values in ``user_dict``
   (exactly what the webapp's POST processing does, minus form decoding),
   marks the screen's question answered, and calls assemble again.
3. When no question is needed any more, assemble raises DAErrorNoEndpoint
   ("finished executing all mandatory code") — that is "interview finished".
"""
from __future__ import annotations

import json
import os
import pickle
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from docassemble_simulator.describe import (
    describe_question_result,
    describe_seeking,
    field_required,
    field_visible,
    from_safeid_safe,
)


class SessionError(Exception):
    pass


class Session:
    def __init__(self, interview_path: str, root: str | Path):
        self.interview_path = interview_path
        self.root = Path(root).resolve()
        self.state_dir = self.root / ".dasimulator"
        self.state_file = self.state_dir / "session.pkl"

    # ------------------------------------------------------------------ setup

    def load_interview(self):
        """Parse the interview YAML through the real docassemble compiler."""
        import sys

        from docassemble.base.interview_cache import get_interview
        from docassemble.base.thread_context import empty_globals, global_context

        saved_argv = list(sys.argv)
        sys.argv[:] = ["docassemble-simulator", self.interview_path]
        try:
            with global_context(empty_globals()):
                interview = get_interview(self.interview_path)
        finally:
            sys.argv[:] = saved_argv
        return interview

    def fresh_user_dict(self) -> dict:
        import datetime

        from docassemble.base.util import DAObject

        try:
            from docassemble.base.functions import DANav

            nav = DANav()
        except Exception:
            nav = DAObject(instanceName="nav", sections=None)

        return {
            "_internal": {
                "gather": [],
                "modtime": datetime.datetime.now(tz=datetime.timezone.utc),
                "tracker": 0,
                "steps": [],
                "question_queue": [],
                "step_order": 0,
                "action": None,
                "args": {},
                "answered": set(),
                "event_stack": {},
                "tasks": {},
                "informed": {},
                "dirty": {},
            },
            "user": {"is_authenticated": True, "roles": ["user"]},
            "session": DAObject(instanceName="session"),
            "M": DAObject(instanceName="M"),
            "nav": nav,
            "url_args": {},
        }

    @staticmethod
    def build_status():
        from docassemble.base.parse import InterviewStatus

        return InterviewStatus(
            current_info={
                "user": {
                    "session_uid": "dasimulator",
                    "is_authenticated": True,
                    "roles": ["user"],
                    "id": 1,
                    "email": "agent@localhost",
                    "device_id": "dasimulator",
                    "the_user_id": "1",
                }
            }
        )

    # ----------------------------------------------------------- persistence

    def save(
        self,
        user_dict: dict,
        screen: dict | None = None,
        *,
        origin: str = "flow",
        sought_variable: str | None = None,
    ) -> None:
        """Persist the session.

        origin='flow': screen came from the mandatory chain (assemble).
        origin='seek': screen came from an explicit `seek`; subsequent
        status/set target THIS screen until it is answered, since the
        mandatory chain may not reach it first.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "interview_path": self.interview_path,
            "user_dict": _picklable_view(user_dict),
            "screen": _jsonable(screen),
            "origin": origin,
            "sought_variable": sought_variable,
        }
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        try:
            with open(tmp, "wb") as fh:
                pickle.dump(payload, fh)
            os.replace(tmp, self.state_file)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def load_state(self) -> tuple[dict, dict | None]:
        user_dict, screen, _origin, _sought = self.load_state_full()
        return user_dict, screen

    def load_state_full(self) -> tuple[dict, dict | None, str, str | None]:
        if not self.state_file.exists():
            raise SessionError(
                f"no saved session at {self.state_file}; run `start` first"
            )
        try:
            with open(self.state_file, "rb") as fh:
                payload = pickle.load(fh)
        except Exception as err:
            raise SessionError(
                f"could not load saved session ({err}); delete {self.state_file} "
                "and run `start` again"
            ) from err
        if payload.get("interview_path") != self.interview_path:
            raise SessionError(
                f"saved session belongs to {payload.get('interview_path')}, "
                f"not {self.interview_path}; rerun `start`"
            )
        return (
            payload["user_dict"],
            payload.get("screen"),
            payload.get("origin", "flow"),
            payload.get("sought_variable"),
        )


    def reset(self) -> None:
        if self.state_file.exists():
            self.state_file.unlink()

    # -------------------------------------------------------------- contexts

    @staticmethod
    @contextmanager
    def _in_interview(interview, user_dict: dict):
        """Enter the thread context every docassemble evaluation expects."""
        from docassemble.base.functions import this_thread
        from docassemble.base.parse import InterviewStatus
        from docassemble.base.thread_context import (
            empty_globals,
            global_context,
            user_dict_context,
        )
        status = InterviewStatus(current_info=Session.build_status().current_info)
        # url_action()/action_menu_item() read this; keep it present.
        status.current_info["yaml_filename"] = interview.source.path
        status.current_info.setdefault("url", None)
        with global_context(empty_globals()), user_dict_context(user_dict):
            this_thread.current_info = status.current_info
            this_thread.interview = interview
            this_thread.interview_status = status
            this_thread.internal = user_dict.get("_internal", {})
            yield status


    def run_prelude(self, user_dict: dict, interview=None) -> None:
        """Execute .dasimulator/prelude.py in the session namespace, if present.

        Use it to stub server-only dependencies (firm DB, PMS OAuth, matter
        lookups) by patching module attributes or seeding variables. Runs
        before the mandatory chain on every flow pass; also callable
        standalone (set/get/exec/vars) so patches apply before validation and
        expression evaluation.
        """
        prelude = self.state_dir / "prelude.py"
        if not prelude.exists():
            return
        code = prelude.read_text(encoding="utf-8")
        try:
            # Own context: safe to nest inside an active one (contextvar
            # tokens restore outer state), and required when called before
            # any assemble pass has set up this_thread.
            if interview is None:
                interview = self.load_interview()
            self.exec_in_namespace(code, user_dict, interview)
        except SessionError:
            raise
        except Exception as err:
            raise SessionError(
                f"prelude script {prelude} failed: {type(err).__name__}: {err}"
            ) from err

    # -------------------------------------------------------------- the flow

    def current_screen(self, user_dict: dict) -> tuple[dict, Any]:
        """Run the mandatory chain; return (screen description, interview)."""
        interview = self.load_interview()
        with self._in_interview(interview, user_dict) as status:
            interview.load_util(user_dict)
            self.run_prelude(user_dict)
            try:
                interview.assemble(user_dict, interview_status=status)
            except Exception as err:
                from docassemble.base.error import DAErrorNoEndpoint

                if isinstance(err, DAErrorNoEndpoint):
                    return {"kind": "finished", "message": _first_line(err)}, interview
                return (
                    {
                        "kind": "error",
                        "error_type": type(err).__name__,
                        "message": str(err),
                        "traceback_tail": _traceback_tail(),
                    },
                    interview,
                )
            if getattr(status, "question", None) is not None:
                if status.question.question_type == "continue":
                    return {
                        "kind": "continue",
                        "message": status.question_text or "continue screen reached",
                        "question_name": getattr(status.question, "name", None),
                    }, interview
                result = {
                    "type": "question",
                    "question_text": status.question_text,
                    "subquestion_text": status.subquestion_text,
                    "continue_label": status.continue_label,
                    "sought": status.sought,
                    "orig_sought": status.orig_sought,
                    "question": status.question,
                }
                return describe_question_result(result, user_dict), interview
            return {
                "kind": "continue",
                "message": "mandatory code completed without reaching a screen",
            }, interview

    def seek(self, user_dict: dict, varname: str, *, trace: bool = False) -> tuple[dict, Any]:
        """Drive askfor() directly for one variable; report the next screen.

        Runs the mandatory chain first (like the original harness did): setup
        blocks must execute before the seek, or conditions referencing
        not-yet-initialized objects fail for the wrong reason.
        """
        interview = self.load_interview()
        with self._in_interview(interview, user_dict) as status:
            interview.load_util(user_dict)
            self.run_prelude(user_dict)
            try:
                interview.assemble(user_dict, interview_status=status)
            except Exception:
                pass  # the flow's own first screen is irrelevant to the seek
            old_dict = dict(user_dict)
            result = interview.askfor(
                varname,
                user_dict,
                old_dict,
                status,
                seeking=[],
                variable_stack=set(),
                questions_tried={},
            )
            out: dict[str, Any] = {"sought_variable": varname}
            if result.get("type") == "question":
                out.update(describe_question_result(result, user_dict))
            elif result.get("type") == "continue":
                out["kind"] = "continue"
                out["message"] = (
                    f"'{varname}' resolved without needing a screen "
                    "(a code/objects/data block defined it)"
                )
            else:
                out["kind"] = result.get("type", "unknown")
                out["raw"] = {
                    k: str(v)[:120] for k, v in result.items() if k != "question"
                }
            if trace:
                out["seeking"] = describe_seeking(status.seeking)
            return out, interview

    # ---------------------------------------------------------------- answers

    def apply_assignments(
        self, interview, user_dict: dict, assignments: list[tuple[str, str]], *, use_code: bool
    ) -> list[str]:
        """Apply VAR=VALUE pairs inside the interview namespace.

        Values are parsed as JSON when possible (true/false/null/numbers/
        quoted strings/lists/objects); anything else is kept as a literal
        string. With use_code=True each value is evaluated as a Python
        expression in the session namespace instead.
        """
        errors: list[str] = []
        with self._in_interview(interview, user_dict):
            for var, raw_value in assignments:
                try:
                    if use_code:
                        command = f"{var} = {raw_value}"
                    else:
                        # repr(), not json.dumps(): the assignment is exec'd as
                        # Python, where booleans are True/False not true/false.
                        command = f"{var} = {parse_value(raw_value)!r}"
                    exec(command, user_dict)
                except Exception as err:
                    errors.append(f"{var}: {type(err).__name__}: {err}")
        return errors

    def mark_answered(self, interview, user_dict: dict, question_name: str | None) -> None:
        """Mirror the webapp: after applying answers, mark the screen answered."""
        if not question_name:
            return
        try:
            question = interview.questions_by_name[question_name]
        except KeyError:
            return
        try:
            question.mark_as_answered(user_dict)
        except Exception:
            pass

    def validate_screen(
        self,
        interview,
        user_dict: dict,
        screen: dict | None,
    ) -> dict:
        """Replay the server's submit-time checks for the screen being answered.

        Order mirrors the webapp: apply values (caller), exec the question's
        `validation code`, then report required fields still undefined.

        Returns {"errors": [...], "warnings": [...]}.

        errors (blocking, like the webapp): the `validation code` raised —
        including DAValidationError with its message. The caller must NOT
        advance or save; the webapp discards submitted values in this case.

        warnings (advisory): required fields still undefined after everything
        ran. The real browser enforces these before POST, so they don't block
        here by default; --strict turns them into errors.
        """
        errors: list[str] = []
        warnings: list[str] = []

        question_name = (screen or {}).get("question_name")
        question = (
            interview.questions_by_name.get(question_name) if question_name else None
        )

        # 1. Question-level validation code.
        validation_code = getattr(question, "validation_code", None) if question else None
        if validation_code is not None:
            from docassemble.base.error import DAValidationError

            with self._in_interview(interview, user_dict):
                try:
                    exec(validation_code, user_dict)
                except DAValidationError as err:
                    field_attr = getattr(err, "field", None)
                    message = str(err) or "validation failed"
                    if isinstance(field_attr, str):
                        errors.append(f"field '{field_attr}': {message}")
                    else:
                        errors.append(message)
                except BaseException as err:
                    errors.append(
                        f"validation code crashed ({type(err).__name__}): {_first_line(err)}"
                    )
        # 2. Required-field check against the LIVE field objects, so show-if
        # visibility is evaluated against the post-answer state.
        raw_fields = getattr(question, "fields", None)
        if raw_fields is None:
            # No live question (e.g. synthesized screen): fall back to the
            # pickled description.
            for f in (screen or {}).get("fields") or []:
                if f.get("visible") is False or f.get("required") is False:
                    continue  # [hidden]/[optional] fields need no answer
                self._check_required_field(
                    user_dict, f.get("variable"), f.get("type"), warnings
                )
        else:
            for field in raw_fields:
                visible, _note = field_visible(field, user_dict)
                if visible is False:
                    continue
                if not field_required(field, user_dict):
                    continue
                varname = from_safeid_safe(getattr(field, "saveas", "") or "")
                if not varname:
                    continue
                if getattr(field, "action", None):
                    continue  # button-style fields carry no value
                datatype = str(getattr(field, "datatype", "") or "").lower()
                if "signature" in datatype:
                    continue  # cannot be satisfied locally
                try:
                    eval(varname, user_dict)
                except Exception:
                    warnings.append(
                        f"required field '{varname}' is undefined "
                        "(the browser would refuse to submit this screen)"
                    )
        return {"errors": errors, "warnings": warnings}

    @staticmethod
    def _check_required_field(
        user_dict: dict, varname: str | None, ftype: Any, warnings: list[str]
    ) -> None:
        if not varname:
            return
        if "signature" in str(ftype or "").lower():
            return
        try:
            eval(varname, user_dict)
        except Exception:
            warnings.append(
                f"required field '{varname}' is undefined "
                "(the browser would refuse to submit this screen)"
            )

    # ---------------------------------------------------------- introspection

    def eval_in_session(self, interview, user_dict: dict, expression: str) -> Any:
        with self._in_interview(interview, user_dict):
            return eval(expression, user_dict)

    def exec_in_namespace(self, code: str, user_dict: dict, interview) -> None:
        """Execute code with the same thread context as an interview pass."""
        with self._in_interview(interview, user_dict):
            exec(code, user_dict)

    def exec_in_session(self, interview, user_dict: dict, code: str) -> None:
        """Backward-compatible name for the session ``exec`` command."""
        self.exec_in_namespace(code, user_dict, interview)


def _first_line(err: BaseException) -> str:
    text = str(err).strip()
    return text.splitlines()[0] if text else type(err).__name__


def _traceback_tail(limit: int = 8) -> str:
    lines = traceback.format_exc().strip().splitlines()
    return "\n".join(lines[-limit:])

def _picklable_view(user_dict: dict) -> dict:
    """Drop top-level entries pickle cannot serialize.

    Interview imports inject callables (e.g. the 'ordinal' word function) that
    are closures and thus unpicklable; assemble() re-runs the imports block on
    every flow pass, so dropping them is safe — identical to what survives a
    server save/load round trip.
    """
    kept = {}
    for key, value in user_dict.items():
        try:
            # loads-side check too: some DA objects (e.g. lazy Verb tables)
            # pickle fine but fail on unpickle.
            pickle.loads(pickle.dumps(value))
        except Exception:
            continue
        kept[key] = value
    return kept


def parse_value(raw: str) -> Any:
    """JSON first (true/false/null), then Python literal (single quotes), then string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        import ast

        value = ast.literal_eval(raw)
        if isinstance(value, (str, int, float, bool, list, dict, tuple, set)) or value is None:
            return value
        return raw
    except Exception:
        return raw




def variable_names(user_dict: dict, limit: int = 200) -> list[str]:
    names = [k for k in user_dict.keys() if k not in ("_internal", "__builtins__")]
    return sorted(names)[:limit]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    return repr(obj)


__all__ = [
    "Session",
    "SessionError",
    "parse_value",
    "variable_names",
]
