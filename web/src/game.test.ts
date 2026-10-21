import { describe, expect, it } from "vitest";
import { Game, groupCode, replay } from "./game";

describe("dots and boxes rules", () => {
  it("starts a 5 by 4 board for player 1", () => {
    const game = new Game();
    expect(game.horizontal).toHaveLength(25);
    expect(game.vertical).toHaveLength(24);
    expect(game.boxes).toHaveLength(20);
    expect(game.currentPlayer).toBe(1);
    expect(game.finished).toBe(false);
    expect(game.winner).toBeNull();
  });

  it("gives the box and an extra turn to the player who closes it", () => {
    const game = replay(1, 1, [
      { orientation: "h", row: 0, col: 0 },
      { orientation: "v", row: 0, col: 0 },
      { orientation: "h", row: 1, col: 0 },
      { orientation: "v", row: 0, col: 1 },
    ]);
    expect(game.finished).toBe(true);
    expect(game.scores).toEqual([0, 1]);
    expect(game.winner).toBe(2);
    expect(game.boxes).toEqual([2]);
  });

  it("rejects an occupied edge without changing the turn", () => {
    const game = new Game(1, 1);
    expect(game.claim("h", 0, 0)).toEqual([]);
    expect(game.claim("h", 0, 0)).toBeNull();
    expect(game.currentPlayer).toBe(2);
    expect(game.moves).toBe(1);
  });

  it("spaces a pairing code for the badge buttons", () => {
    expect(groupCode("ABCAB")).toBe("A B C A B");
  });
});
