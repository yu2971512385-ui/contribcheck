"""Tests for contribcheck. The GitHub API is stubbed; nothing hits the network."""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import urllib.error

import pytest

import contribcheck as cc


class FakeGitHub(cc.GitHub):
    """Answers from a dict instead of the network."""

    def __init__(self, files: dict[str, str] | None = None, searches: dict[str, dict] | None = None,
                 meta: dict | None = None) -> None:
        super().__init__(token=None)
        self.files = files or {}
        self.searches = searches or {}
        self.meta = meta
        self.asked: list[str] = []

    def get(self, path, **params):
        self.asked.append(path + (("?" + json.dumps(params, sort_keys=True)) if params else ""))
        if path.startswith("/repos/") and "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name not in self.files:
                return None
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.files[name].encode()).decode(),
            }
        if path == "/search/issues":
            for fragment, payload in self.searches.items():
                if fragment in params.get("q", ""):
                    return payload
            return {"total_count": 0, "items": []}
        if path.startswith("/repos/"):
            return self.meta
        return None


def meta(**overrides) -> dict:
    base = {
        "full_name": "owner/repo",
        "description": "a repository",
        "stargazers_count": 100,
        "pushed_at": "2026-10-01T00:00:00Z",
        "archived": False,
    }
    base.update(overrides)
    return base


class TestPolicies:
    def test_finds_a_refusal_of_autonomous_agents(self):
        api = FakeGitHub(files={"AI_POLICY.md": "Autonomous agents are not allowed to contribute.\n"})
        found = cc.find_policies(api, "owner/repo")
        assert ("AI_POLICY.md", "ai", "refuses autonomous agents") in [f[:3] for f in found]

    def test_finds_a_disclosure_requirement(self):
        api = FakeGitHub(files={"CONTRIBUTING.md": "Any AI assistance must be disclosed in the PR.\n"})
        meanings = [f[2] for f in cc.find_policies(api, "owner/repo")]
        assert "requires AI assistance to be disclosed" in meanings

    def test_finds_a_cla(self):
        api = FakeGitHub(files={"CONTRIBUTING.md": "You must sign our Contributor License Agreement.\n"})
        kinds = [f[1] for f in cc.find_policies(api, "owner/repo")]
        assert "legal" in kinds

    def test_finds_an_issue_first_rule(self):
        api = FakeGitHub(files={"AGENTS.md": "Please open an issue before a pull request.\n"})
        meanings = [f[2] for f in cc.find_policies(api, "owner/repo")]
        assert "asks for an issue before a pull request" in meanings

    def test_reports_the_line_it_matched(self):
        line = "Autonomous agents are not allowed here."
        api = FakeGitHub(files={"AGENTS.md": f"preamble\n\n{line}\n"})
        assert cc.find_policies(api, "owner/repo")[0][3] == line

    def test_says_nothing_about_a_repository_without_rules(self):
        api = FakeGitHub(files={"CONTRIBUTING.md": "Run the tests before opening a pull request.\n"})
        assert cc.find_policies(api, "owner/repo") == []

    def test_does_not_repeat_the_same_finding_within_a_file(self):
        text = "\n".join(["We use AI here."] * 5)
        api = FakeGitHub(files={"CONTRIBUTING.md": text})
        assert len(cc.find_policies(api, "owner/repo")) == 1


class TestMergeActivity:
    def test_counts_and_medians(self):
        items = [
            {"created_at": "2026-10-01T00:00:00Z", "closed_at": "2026-10-03T00:00:00Z"},
            {"created_at": "2026-10-01T00:00:00Z", "closed_at": "2026-10-05T00:00:00Z"},
        ]
        api = FakeGitHub(searches={
            "is:merged": {"total_count": 2, "items": items},
            "is:open": {"total_count": 7, "items": []},
        })
        activity = cc.merge_activity(api, "owner/repo", 6)
        assert activity["merged"] == 2
        assert activity["open"] == 7
        assert activity["median_days"] == pytest.approx(3.0)

    def test_excludes_bots_from_the_query(self):
        api = FakeGitHub(searches={"is:merged": {"total_count": 0, "items": []}})
        cc.merge_activity(api, "owner/repo", 6)
        merged_query = next(a for a in api.asked if "is%3Amerged" in a or "is:merged" in a)
        assert "-author:app/dependabot" in merged_query

    def test_no_merges_leaves_the_median_empty(self):
        api = FakeGitHub(searches={})
        assert cc.merge_activity(api, "owner/repo", 6)["median_days"] is None


def test_months_ago_crosses_a_year_boundary():
    value = cc.months_ago(12)
    assert value < dt.date.today().isoformat()
    assert len(value) == 10


class TestReport:
    def test_archived_repository_is_called_out(self, capsys):
        api = FakeGitHub(meta=meta(archived=True))
        assert cc.report("owner/repo", api, 6, None) == 1
        assert "will not take a pull request" in capsys.readouterr().out

    def test_missing_repository(self, capsys):
        api = FakeGitHub(meta=None)
        assert cc.report("owner/repo", api, 6, None) == 2
        assert "no such repository" in capsys.readouterr().err

    def test_flags_a_backlog_much_larger_than_the_merge_rate(self, capsys):
        api = FakeGitHub(
            meta=meta(),
            searches={
                "is:merged": {"total_count": 2, "items": []},
                "is:open": {"total_count": 400, "items": []},
            },
        )
        cc.report("owner/repo", api, 6, None)
        assert "backlog is much larger" in capsys.readouterr().out

    def test_duplicate_search_covers_issues_and_pull_requests(self, capsys):
        api = FakeGitHub(
            meta=meta(),
            searches={
                "is:issue": {"total_count": 1, "items": [
                    {"number": 7, "state": "open", "title": "already reported"}
                ]},
                "is:pr": {"total_count": 1, "items": [
                    {"number": 8, "state": "closed", "title": "already fixed",
                     "pull_request": {"merged_at": "2026-01-01T00:00:00Z"}}
                ]},
            },
        )
        cc.report("owner/repo", api, 6, "a bug")
        out = capsys.readouterr().out
        assert "#7 [open] already reported" in out
        # A merged pull request should not read as merely "closed".
        assert "#8 [merged] already fixed" in out


class TestErrors:
    def _http_error(self, code: int, message: str, headers: dict | None = None):
        return urllib.error.HTTPError(
            "https://api.github.com/x", code, "err", headers or {},
            io.BytesIO(json.dumps({"message": message}).encode()),
        )

    def test_message_comes_from_the_body(self):
        error = self._http_error(403, "API rate limit exceeded")
        assert cc._message_of(error) == "API rate limit exceeded"

    def test_secondary_limit_is_retried(self, monkeypatch):
        api = cc.GitHub(token=None)
        attempts = []

        def flaky(url):
            attempts.append(url)
            if len(attempts) < 3:
                raise self._http_error(403, "You have exceeded a secondary rate limit")
            return {"ok": True}

        monkeypatch.setattr(api, "_request", flaky)
        monkeypatch.setattr(cc.time, "sleep", lambda _: None)
        assert api.get("/x") == {"ok": True}
        assert len(attempts) == 3

    def test_a_real_rate_limit_stops_with_advice(self, monkeypatch):
        api = cc.GitHub(token=None)
        monkeypatch.setattr(
            api, "_request",
            lambda url: (_ for _ in ()).throw(self._http_error(403, "API rate limit exceeded")),
        )
        with pytest.raises(SystemExit) as caught:
            api.get("/x")
        assert "gh auth login" in str(caught.value)

    def test_missing_resource_is_none_not_an_error(self, monkeypatch):
        api = cc.GitHub(token=None)
        monkeypatch.setattr(
            api, "_request",
            lambda url: (_ for _ in ()).throw(self._http_error(404, "Not Found")),
        )
        assert api.get("/x") is None


def test_cli_rejects_a_bare_name(capsys):
    assert cc.main(["notarepo"]) == 2
    assert "expected OWNER/REPO" in capsys.readouterr().err


def test_a_dropped_connection_is_retried_then_reported(monkeypatch):
    api = cc.GitHub(token=None)
    attempts = []

    def flaky(url):
        attempts.append(url)
        if len(attempts) < 3:
            raise ConnectionResetError("Remote end closed connection without response")
        return {"ok": True}

    monkeypatch.setattr(api, "_request", flaky)
    monkeypatch.setattr(cc.time, "sleep", lambda _: None)
    assert api.get("/x") == {"ok": True}

    always = cc.GitHub(token=None)
    monkeypatch.setattr(
        always, "_request",
        lambda url: (_ for _ in ()).throw(ConnectionResetError("gone")),
    )
    with pytest.raises(SystemExit) as caught:
        always.get("/x")
    assert "could not reach github" in str(caught.value)
