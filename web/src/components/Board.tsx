import { replay, type Move } from "../game";

const PLAYERS = ["#2c3a37", "#3ecf8e", "#6da9ff"];

export function BoardView({
  cols,
  rows,
  moves,
  onEdge,
  disabled = false,
  selected,
}: {
  cols: number;
  rows: number;
  moves: Move[];
  onEdge?: (edge: Move) => void;
  disabled?: boolean;
  selected?: Move;
}) {
  const game = replay(cols, rows, moves);
  const step = 72;
  const left = 36;
  const top = 28;
  const width = left * 2 + cols * step;
  const height = top * 2 + rows * step;

  function edgeTarget(orientation: "h" | "v", row: number, col: number, owner: number, x: number, y: number) {
    if (!onEdge || owner) return null;
    const highlighted = selected?.orientation === orientation && selected.row === row && selected.col === col;
    const edge = { orientation, row, col };
    return (
      <g className={disabled ? "edge-control" : "edge-control enabled"}>
        <line
          className="edge-highlight"
          x1={x} y1={y} x2={x + (orientation === "h" ? step : 0)} y2={y + (orientation === "v" ? step : 0)}
          stroke={highlighted ? "#f5cb68" : "transparent"} strokeWidth="12" strokeLinecap="square" pointerEvents="none"
        />
        <rect
          className="edge-target"
          x={x - (orientation === "v" ? 14 : 0)} y={y - (orientation === "h" ? 14 : 0)}
          width={orientation === "h" ? step : 28} height={orientation === "v" ? step : 28}
          fill="transparent"
          role="button"
          tabIndex={disabled ? -1 : 0}
          aria-disabled={disabled}
          aria-label={`Claim ${orientation === "h" ? "horizontal" : "vertical"} edge row ${row + 1} column ${col + 1}`}
          onClick={() => { if (!disabled) onEdge(edge); }}
          onKeyDown={(event) => {
            if (!disabled && (event.key === "Enter" || event.key === " ")) {
              event.preventDefault();
              onEdge(edge);
            }
          }}
        />
      </g>
    );
  }

  return (
    <svg className="board" viewBox={`0 0 ${width} ${height}`} role={onEdge ? "group" : "img"} aria-label="Dots and boxes board">
      <rect width={width} height={height} fill="#101818" />
      {Array.from({ length: rows }, (_, row) =>
        Array.from({ length: cols }, (_, col) => {
          const owner = game.boxes[row * cols + col];
          if (!owner) return null;
          return (
            <g key={`box-${row}-${col}`}>
              <rect
                x={left + col * step + 8}
                y={top + row * step + 8}
                width={step - 16}
                height={step - 16}
                rx="0"
                fill={owner === 1 ? "#183b2c" : "#172d48"}
              />
              <text
                x={left + col * step + step / 2}
                y={top + row * step + step / 2 + 8}
                textAnchor="middle"
                fill={PLAYERS[owner]}
                fontSize="24"
                fontFamily="Silkscreen, monospace"
              >
                {owner}
              </text>
            </g>
          );
        }),
      )}
      {Array.from({ length: rows + 1 }, (_, row) =>
        Array.from({ length: cols }, (_, col) => {
          const owner = game.horizontal[row * cols + col];
          const x = left + col * step;
          const y = top + row * step;
          return (
            <g key={`h-${row}-${col}`}>
              <line
                x1={x}
                y1={y}
                x2={x + step}
                y2={y}
                stroke={PLAYERS[owner]}
                strokeWidth={owner ? 8 : 2}
                strokeLinecap="square"
              />
              {edgeTarget("h", row, col, owner, x, y)}
            </g>
          );
        }),
      )}
      {Array.from({ length: rows }, (_, row) =>
        Array.from({ length: cols + 1 }, (_, col) => {
          const owner = game.vertical[row * (cols + 1) + col];
          const x = left + col * step;
          const y = top + row * step;
          return (
            <g key={`v-${row}-${col}`}>
              <line
                x1={x}
                y1={y}
                x2={x}
                y2={y + step}
                stroke={PLAYERS[owner]}
                strokeWidth={owner ? 8 : 2}
                strokeLinecap="square"
              />
              {edgeTarget("v", row, col, owner, x, y)}
            </g>
          );
        }),
      )}
      {Array.from({ length: rows + 1 }, (_, row) =>
        Array.from({ length: cols + 1 }, (_, col) => (
          <rect key={`d-${row}-${col}`} x={left + col * step - 5} y={top + row * step - 5} width="10" height="10" fill="#b8cdc3" />
        )),
      )}
    </svg>
  );
}
