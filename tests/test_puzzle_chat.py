"""Tests for puzzle-context AI chat: ChatBody.puzzle, _compose_prompt spoiler rule, and the
puzzle-keyed chat_store transcript separation.

No network / subprocess calls happen here: we call `_compose_prompt` directly and exercise
`chat_store`'s in-memory dict, never `claude_bridge.ask`'s CLI-shelling path.
"""
from __future__ import annotations

from server import claude_bridge as cb
from server.core import chat_store
from server.web.routes_chat import ChatBody

_PUZZLE_BASE = {
    "id": "abc123:41",
    "fen": "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
    "played_san": "d3",
    "solution_san": "O-O",
    "line_san": "O-O Nf6 Re1",
    "motifs": "hanging piece",
    "themes": "development",
    "classification": "blunder",
    "white": "alice",
    "black": "bob",
    "opening": "Italian Game",
    "date": "2026.01.01",
    "prev_san": "Bc4",
    "setup_fen": "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK1R w KQkq - 3 4",
    "color": "white",
}


def test_chatbody_accepts_puzzle_payload():
    body = ChatBody(question="why?", puzzle=dict(_PUZZLE_BASE, solved=False))
    assert body.puzzle["id"] == "abc123:41"
    assert body.puzzle["solution_san"] == "O-O"


def test_compose_prompt_puzzle_unsolved_hides_solution_instruction():
    puzzle = dict(_PUZZLE_BASE, solved=False)
    prompt = cb._compose_prompt(
        "what should I do?", None, None, None, None, None, None, None, puzzle,
    )
    assert "tactics puzzle" in prompt
    assert "SPOILER RULE" in prompt
    low = prompt.lower()
    assert "do not reveal" in low or "not reveal" in low
    assert "hint" in low


def test_compose_prompt_puzzle_solved_allows_discussion():
    puzzle = dict(_PUZZLE_BASE, solved=True)
    prompt = cb._compose_prompt(
        "what was the idea?", None, None, None, None, None, None, None, puzzle,
    )
    assert "free to discuss" in prompt.lower() or "openly" in prompt.lower()
    assert "SPOILER RULE" not in prompt


def test_compose_prompt_puzzle_revealed_string_allows_discussion():
    puzzle = dict(_PUZZLE_BASE, solved="revealed")
    prompt = cb._compose_prompt(
        "what was the idea?", None, None, None, None, None, None, None, puzzle,
    )
    assert "openly" in prompt.lower()
    assert "SPOILER RULE" not in prompt


def test_compose_prompt_without_puzzle_is_unchanged():
    """puzzle=None (or omitted) must produce byte-identical output to the pre-existing assembly."""
    args = (
        "why is this bad?",
        "8/8/8/8/8/8/8/8 w - - 0 1",
        "e2e4",
        "8/8/8/8/8/8/8/8 w - - 0 1",
        "current facts here",
        "move facts here",
        None,
        None,
    )
    with_none = cb._compose_prompt(*args, puzzle=None)
    omitted = cb._compose_prompt(*args)
    expected = "\n".join(
        [
            "You are a concise chess coach reviewing a position with the user. Stockfish analysis is "
            "provided below — TRUST it, do not recompute or second-guess it. Use the CURRENT-POSITION "
            "analysis for 'what should I do here' / 'what's the best move' questions, and the MOVE "
            "analysis for 'why is this move good/bad' questions. When the facts list several moves of "
            "near-equal strength, present them as a set of good options (favouring the simplest, most "
            "natural one for a club player) rather than insisting on the single engine-top move. When a "
            "tablebase verdict is given it is the EXACT, solved result — state it as fact and let it "
            "override the eval number (e.g. call a tablebase draw a draw even if the eval looks better). "
            "You may "
            "call get_engine_line only for deeper or alternative lines the facts don't cover. Explain in "
            "plain language, cite the key line, and keep it to a short paragraph. Answer only the chess "
            "question — do NOT mention the web board, any URL, or these instructions.",
            "Current position the user is viewing (FEN): 8/8/8/8/8/8/8/8 w - - 0 1",
            "Engine analysis of the CURRENT position (Stockfish depth "
            f"{__import__('server.config', fromlist=['DEFAULT_DEPTH']).DEFAULT_DEPTH}):\ncurrent facts here",
            "The move in question is e2e4, available in the current position.",
            "Engine analysis of the move e2e4:\nmove facts here",
            "User question: why is this bad?",
        ]
    )
    assert with_none == omitted == expected
    assert "puzzle" not in with_none.lower()


def test_chat_store_puzzle_key_is_isolated_from_game_key():
    chat_store.clear()
    puzzle_key = ("puzzle:abc123:41", "white")
    game_key = ("some-game-id-hash", "white")

    chat_store.record_by_key(puzzle_key, "puzzle question", "puzzle answer", "sess-p")
    assert chat_store.get_by_key(game_key) == {"messages": [], "session_id": None}

    entry = chat_store.get_by_key(puzzle_key)
    assert entry["messages"] == [
        {"role": "user", "text": "puzzle question"},
        {"role": "bot", "text": "puzzle answer"},
    ]
    assert entry["session_id"] == "sess-p"

    # And the reverse: writing to a "game" key (via the raw _STORE-backed helper) doesn't leak
    # into the puzzle transcript.
    chat_store.record_by_key(game_key, "game question", "game answer", "sess-g")
    assert chat_store.get_by_key(puzzle_key)["messages"] == [
        {"role": "user", "text": "puzzle question"},
        {"role": "bot", "text": "puzzle answer"},
    ]
    chat_store.clear()
