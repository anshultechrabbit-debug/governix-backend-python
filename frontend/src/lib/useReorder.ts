import { useState, type DragEvent, type KeyboardEvent } from "react";

/**
 * Drag-and-drop reordering of a list, with a visible drop line and keyboard
 * moves (Alt+Up / Alt+Down on the handle). The caller persists the new order.
 */
export function useReorder<T extends { id: string }>(
  items: T[],
  onReorder: (next: T[], movedId: string) => void,
  enabled = true,
) {
  const [dragId, setDragId] = useState<string | null>(null);
  const [over, setOver] = useState<{ id: string; after: boolean } | null>(null);

  function move(id: string, targetId: string, after: boolean) {
    const from = items.findIndex((i) => i.id === id);
    if (from < 0 || id === targetId) return;
    const rest = items.filter((i) => i.id !== id);
    let to = rest.findIndex((i) => i.id === targetId);
    if (to < 0) return;
    if (after) to += 1;
    const next = [...rest.slice(0, to), items[from], ...rest.slice(to)];
    if (next.some((item, index) => item.id !== items[index].id)) onReorder(next, id);
  }

  function reset() {
    setDragId(null);
    setOver(null);
  }

  return {
    dragId,
    /** Where the dragged row would land, for the drop line. */
    dropLine: (id: string): "before" | "after" | null =>
      dragId && over?.id === id && dragId !== id ? (over.after ? "after" : "before") : null,
    rowProps: (item: T) => enabled ? {
      draggable: true,
      onDragStart: (event: DragEvent) => {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", item.id);
        setDragId(item.id);
      },
      onDragEnd: reset,
      onDragOver: (event: DragEvent<HTMLElement>) => {
        if (!dragId) return;
        event.preventDefault();
        const box = event.currentTarget.getBoundingClientRect();
        const after = event.clientY > box.top + box.height / 2;
        if (over?.id !== item.id || over.after !== after) setOver({ id: item.id, after });
      },
      onDrop: (event: DragEvent) => {
        event.preventDefault();
        if (dragId && over) move(dragId, over.id, over.after);
        reset();
      },
    } : {},
    handleProps: (item: T, label: string) => ({
      tabIndex: enabled ? 0 : -1,
      role: "button" as const,
      "aria-label": `Reorder ${label}. Alt+Up or Alt+Down to move.`,
      "aria-disabled": !enabled,
      onKeyDown: (event: KeyboardEvent) => {
        if (!enabled || !event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
        event.preventDefault();
        const index = items.findIndex((i) => i.id === item.id);
        const target = items[index + (event.key === "ArrowUp" ? -1 : 1)];
        if (target) move(item.id, target.id, event.key === "ArrowDown");
      },
    }),
  };
}
