/* ResizeEdges — pencere çevresine görünmez 6px tutamaçlar; pointerdown →
   bridge window.startSystemResize(edge). chrome.py _ResizeFilter kenar matematiğinin
   JS portu. Maximize'dayken render edilmez. */

import { bridge, ResizeEdge } from "@/bridge";

const EDGES: { edge: ResizeEdge; style: React.CSSProperties; cursor: string }[] = [
  { edge: "top", style: { top: 0, left: 8, right: 8, height: 6 }, cursor: "ns-resize" },
  { edge: "bottom", style: { bottom: 0, left: 8, right: 8, height: 6 }, cursor: "ns-resize" },
  { edge: "left", style: { left: 0, top: 8, bottom: 8, width: 6 }, cursor: "ew-resize" },
  { edge: "right", style: { right: 0, top: 8, bottom: 8, width: 6 }, cursor: "ew-resize" },
  { edge: "topleft", style: { top: 0, left: 0, width: 10, height: 10 }, cursor: "nwse-resize" },
  { edge: "topright", style: { top: 0, right: 0, width: 10, height: 10 }, cursor: "nesw-resize" },
  { edge: "bottomleft", style: { bottom: 0, left: 0, width: 10, height: 10 }, cursor: "nesw-resize" },
  { edge: "bottomright", style: { bottom: 0, right: 0, width: 10, height: 10 }, cursor: "nwse-resize" },
];

export function ResizeEdges({ maximized }: { maximized: boolean }) {
  if (maximized) return null;
  return (
    <>
      {EDGES.map(({ edge, style, cursor }) => (
        <div
          key={edge}
          data-no-drag
          style={{ position: "fixed", zIndex: 100, cursor, ...style }}
          onPointerDown={(e) => {
            if (e.button !== 0) return;
            e.preventDefault();
            void bridge.call("window.startSystemResize", { edge });
          }}
        />
      ))}
    </>
  );
}
