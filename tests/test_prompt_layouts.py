"""What prompt_box reads from each screen layout Claude Code 2.1.310 and Codex can draw.

The screens are reconstructed from each harness's own rendering code (dim text, cursor and rule widths as drawn).
A wake-up types only into ("box", text); "dialog" holds it, "unknown" fails it after a minute. Every row
pins today's answer, so a change to prompt_box shows exactly which layouts it moves.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import wake

W: int = 80
R: str = "─" * W
INV: str = "\x1b[7m \x1b[0m"  # Claude's block cursor


def dim(s: str) -> str:
    return f"\x1b[2m{s}\x1b[0m"


PAST: str = (
    "❯ fix the flaky test"  # a past prompt, echoed in the transcript above the box
)
REPLY: str = "⏺ Done. All tests pass."
FOOT: str = "  " + dim("? for shortcuts")
TOP: str = f"{PAST}\n\n{REPLY}\n\n"
GPT: str = "  gpt-6.1-sol medium · /tmp/work"

LAYOUTS: dict[str, tuple[str, tuple[str, str]]] = {
    # Claude's prompt box
    "claude empty": (f"{TOP}{R}\n❯ {INV}\n{R}\n{FOOT}", ("box", "")),
    "claude draft": (
        f"{TOP}{R}\n❯ check the logs\n{R}\n{FOOT}",
        ("box", "check the logs"),
    ),
    "claude bash mode": (f"{TOP}{R}\n! ls -la\n{R}\n  shell mode", ("box", "! ls -la")),
    "claude vim insert": (
        f"{TOP}{R}\n❯ hi\n{R}\n  " + dim("-- INSERT --"),
        ("box", "hi"),
    ),
    "claude pasted placeholder": (
        f"{R}\n❯ [Pasted text #1 +20 lines]\n{R}\n{FOOT}",
        ("box", "[Pasted text #1 +20 lines]"),
    ),
    "claude autocomplete below": (
        f"{R}\n❯ look at @src/fo\n{R}\n  src/foo.py\n  src/food.py",
        ("box", "look at @src/fo"),
    ),
    "claude exit confirm": (
        f"{R}\n❯ {INV}\n{R}\n  " + dim("Press Ctrl-C again to exit"),
        ("box", ""),
    ),
    # a renamed session: the name sits at the right of the top rule, one rule char after it
    "claude banner empty": (
        f"{TOP}{'─' * 69} agentbus ─\n❯ {INV}\n{R}\n{FOOT}",
        ("box", ""),
    ),
    "claude banner draft": (
        f"{TOP}{'─' * 69} agentbus ─\n❯ check the logs\n{R}\n{FOOT}",
        ("box", "check the logs"),
    ),
    # a new or /cleared session draws at the top of a tall pane, above empty rows
    "claude fresh session in a tall pane": (
        "welcome\n\n" + f"{R}\n❯ {INV}\n{R}\n{FOOT}" + "\n" * 60,
        ("box", ""),
    ),
    # prompt suggestions: the first letter inverted under the cursor, the rest dim (as tmux captures it)
    "claude suggestion placeholder": (
        f'{TOP}{R}\n❯ \x1b[7mT\x1b[0;2mry "fix lint errors"\x1b[0m\n{R}\n{FOOT}',
        ("box", ""),
    ),
    "claude suggestion placeholder, separate resets": (
        f"{TOP}{R}\n❯ \x1b[7mr\x1b[27m\x1b[2mun the tests\x1b[22m\n{R}\n{FOOT}",
        ("box", ""),
    ),
    "claude suggestion placeholder wrapped in a narrow pane": (
        f"{R}\n❯ \x1b[7mr\x1b[0;2mun the tests and then\x1b[0m\n  \x1b[2mfix what fails\x1b[0m\n{R}\n{FOOT}",
        ("box", ""),
    ),
    # the cursor on a typed letter is draft text
    "claude draft, cursor mid-word": (
        f"{TOP}{R}\n❯ he\x1b[7ml\x1b[0mlo\n{R}\n{FOOT}",
        ("box", "hello"),
    ),
    # Claude without its ❯ in the live box: never fall back to a past prompt in the scrollback
    "claude external editor": (
        f"{TOP}{R}\n"
        + dim("        Save and close editor to continue...")
        + f"\n{R}\n{FOOT}",
        ("unknown", ""),
    ),
    "claude teammate prefix": (
        f"{TOP}{R}\n@worker {INV}\n{R}\n{FOOT}",
        ("unknown", ""),
    ),
    "claude teammate prefix, footer-like reply": (
        f"{PAST}\n\n{REPLY}\n  codex-mini · 0.164 installed\n\n{R}\n@worker {INV}\n{R}\n{FOOT}",
        ("unknown", ""),
    ),
    # Ctrl-R: Enter runs the matched old prompt, not what a paste would add
    "claude history search": (
        f"{TOP}{R}\n❯ fix the flaky test\n{R}\n  "
        + dim("search prompts:")
        + " "
        + dim("flaky"),
        ("dialog", "a menu or search is open"),
    ),
    "claude history search, no match": (
        f"{TOP}{R}\n❯ fix the flaky test\n{R}\n  "
        + dim("no matching prompt:")
        + " "
        + dim("zzz"),
        ("dialog", "a menu or search is open"),
    ),
    # the same words inside a draft are draft text
    "claude draft mentioning menu hints": (
        f"{TOP}{R}\n❯ note:\n  Enter to confirm is the hint\n  search prompts: too\n{R}\n{FOOT}",
        ("box", "note:\n  Enter to confirm is the hint\n  search prompts: too"),
    ),
    # dialogs drawn instead of the box
    "claude permission dialog": (
        f"{PAST}\n\n{R}\n Bash command\n\n   rm -rf build\n\n Do you want to proceed?\n ❯ 1. Yes\n"
        "   2. Yes, and don't ask again\n   3. No, and tell Claude what to do differently (esc)\n\n "
        + dim("Esc to cancel · Tab to amend"),
        ("dialog", "a choice is open"),
    ),
    "claude permission dialog, option 3": (
        (
            f"{PAST}\n\n{R}\n Bash command\n\n   rm -rf build\n\n Do you want to proceed?\n   1. Yes\n"
            "   2. Yes, and don't ask again\n ❯ 3. No, and tell Claude what to do differently (esc)"
        ),
        ("dialog", "a choice is open"),
    ),
    "claude plan approval": (
        (
            f"{PAST}\n\n Here is Claude's plan:\n╭{'─' * 78}╮\n│ step 1{' ' * 71}│\n╰{'─' * 78}╯\n\n"
            " Would you like to proceed?\n\n ❯ 1. Yes, and use auto mode\n   2. Yes, manually approve edits\n"
            "   3. No, keep planning"
        ),
        ("dialog", "a choice is open"),
    ),
    "claude resume picker": (
        f"{PAST}\n\n Resume Session\n╭{'─' * 40}╮\n│ ⌕ Search…{' ' * 30}│\n╰{'─' * 40}╯\n"
        "❯ fix the flaky test\n  2 hours ago · 15 messages\n  other session\n  "
        + dim("↑/↓ to navigate · Enter to select · Esc to exit"),
        ("unknown", ""),
    ),
    "claude menu framed by rules": (
        f"{PAST}\n\n{R}\n❯ Default (recommended)\n  Opus\n{R}\n  "
        + dim("Enter to confirm · Esc to exit"),
        ("dialog", "a menu or search is open"),
    ),
    "claude numbered menu framed by rules": (
        f"{PAST}\n\n{R}\n❯ 1. Default (recommended)\n  2. Opus\n{R}\n  "
        + dim("Enter to confirm · Esc to exit"),
        ("dialog", "a menu or search is open"),
    ),
    "claude menu, select and go back hints": (
        f"{PAST}\n\n{R}\n❯ Default (recommended)\n  Opus\n{R}\n  "
        + dim("Enter to select · Esc to go back"),
        ("dialog", "a menu or search is open"),
    ),
    "claude numbered menu, continue hint": (
        f"{PAST}\n\n{R}\n❯ 1. Default\n  2. Opus\n{R}\n  " + dim("Enter to continue"),
        ("dialog", "a menu or search is open"),
    ),
    "claude question with typed answer": (
        f"{PAST}\n\n ☐ Color\n\n Which color?\n\n   1. Red\n   2. Blue\n ❯ 3. green please",
        ("dialog", "a choice is open"),
    ),
    # Codex's composer
    "codex empty": (
        "• done\n\n› "
        + dim("Ask Codex to do anything")
        + "\n\n  "
        + dim("? for shortcuts")
        + "                       100% context left",
        ("box", ""),
    ),
    "codex draft": (f"• done\n\n› fix it\n\n{GPT}", ("box", "fix it")),
    "codex numbered draft": (
        f"› 1. Review the code\n  2. Run the tests\n\n{GPT}",
        ("box", "1. Review the code\n  2. Run the tests"),
    ),
    "codex draft with dialog words": (
        f"› Do you want to review this?\n  Explain Esc to cancel\n\n{GPT}",
        ("box", "Do you want to review this?\n  Explain Esc to cancel"),
    ),
    "codex draft quoting footer text": (
        f"› review this:\n  80% context left\n  ? for shortcuts\n  gpt-6.1-sol · /quoted\n  unfinished explanation\n\n{GPT}",
        (
            "box",
            "review this:\n  80% context left\n  ? for shortcuts\n  gpt-6.1-sol · /quoted\n  unfinished explanation",
        ),
    ),
    "codex draft ending with footer text and blank lines": (
        f"› review this:\n  \n  80% context left\n  \n\n{GPT}",
        ("box", "review this:\n  \n  80% context left\n  "),
    ),
    "codex footer on multiple rows": (
        f"› 1. Review the code\n\n  ? for shortcuts\n{GPT}\n  80% context left",
        ("box", "1. Review the code"),
    ),
    "codex quoted status without a footer spacer": (
        "› review this:\n  80% context left\n  unfinished explanation",
        ("unknown", ""),
    ),
    # a pasted shell prompt inside a Codex draft is draft text, not the prompt
    "codex draft mentioning a menu hint": (
        f"• done\n\n› grep for\n  search prompts: in the ui\n\n{GPT}",
        ("box", "grep for\n  search prompts: in the ui"),
    ),
    "codex draft with a pasted prompt line": (
        f"• done\n\n› look:\n  › git status\n  why is it dirty\n\n{GPT}",
        ("box", "look:\n  › git status\n  why is it dirty"),
    ),
    "codex draft ending in a copied Claude prompt": (
        f"• done\n\n› unfinished draft\n  ❯\n\n{GPT}",
        ("box", "unfinished draft\n  ❯"),
    ),
    "codex command ending in a copied Claude prompt": (
        f"› /clear\n  ❯\n\n{GPT}",
        ("box", "/clear\n  ❯"),
    ),
    "codex draft quoting a Claude menu": (
        f"› look:\n  ❯ 1. Yes\n\n{GPT}",
        ("box", "look:\n  ❯ 1. Yes"),
    ),
    "claude draft ending in a copied prompt": (
        f"{R}\n❯ unfinished draft\n  ❯\n{R}\n{FOOT}",
        ("box", "unfinished draft\n  ❯"),
    ),
    "codex shell mode": (
        "• done\n\n!ls\n\n  Shell mode · gpt-6.1-sol medium · /tmp/work",
        ("unknown", ""),
    ),
    # shell mode with its ! row scrolled out of view: the › above is an old question, not the composer
    "codex shell mode, label on a later footer row": (
        "• old output\n› earlier question\n\n  gpt-5.5 high · 80% left\n  Shell mode",
        ("unknown", ""),
    ),
    "codex shell mode, label below the shortcuts hint": (
        "› earlier question\n  printf hi\n\n  ? for shortcuts\n  Shell mode · 80% left",
        ("unknown", ""),
    ),
    "codex approval": (
        "  Would you like to run the following command?\n\n  $ rm -rf build\n\n› 1. Yes, proceed (y)\n"
        "  2. Yes, and don't ask again (a)\n  3. No, and tell Codex what to do differently (esc)\n\n  "
        + dim("Press enter to confirm or esc to cancel"),
        ("dialog", "a choice is open"),
    ),
    "codex slash popup": (
        f"› /mo\n  /model   choose what model and reasoning effort to use\n  /mention mention a file\n\n{GPT}",
        (
            "box",
            "/mo\n  /model   choose what model and reasoning effort to use\n  /mention mention a file",
        ),
    ),
    "codex backtrack hint": (
        "› "
        + dim("Ask Codex to do anything")
        + "\n\n  "
        + dim("esc again to edit previous message")
        + "        100% context left",
        ("box", ""),
    ),
    "codex footer all dim": (
        "• done\n\n› hi\n\n  " + dim(GPT.strip()),
        ("unknown", ""),
    ),
}


@pytest.mark.parametrize("screen,expected", LAYOUTS.values(), ids=list(LAYOUTS))
def test_prompt_box_layouts(screen: str, expected: tuple[str, str]) -> None:
    with patch.object(wake, "tmux_run", return_value=screen):
        assert wake.prompt_box("/fake", "%1") == expected


@pytest.mark.parametrize("action", ["cancel", "go back"])
@pytest.mark.parametrize("prefix", ["Explain ", "Review this terminal hint:\n  "])
def test_codex_draft_can_quote_menu_instructions(action: str, prefix: str) -> None:
    draft = prefix + f'"Press enter to confirm or esc to {action}"'
    with patch.object(wake, "tmux_run", return_value=f"› {draft}\n\n{GPT}"):
        assert wake.prompt_box("/fake", "%1") == ("box", draft)
