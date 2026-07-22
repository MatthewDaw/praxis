import { useMemo, useState } from "react";
import type { GraphNode, ScopeGroup } from "../../types/graph";

interface ScopeTreeProps {
  scopeGroups?: ScopeGroup[];
  /** Graph nodes, used to resolve member ids to their human-readable labels. */
  nodes?: GraphNode[];
  selectedId: string | null;
  onSelectNode: (id: string) => void;
}

// Group children by parent id once so each node is an O(1) map lookup rather
// than an O(groups) rescan on every render. Roots are keyed under null.
function buildChildrenByParent(
  groups: ScopeGroup[],
): Map<string | null, ScopeGroup[]> {
  const byParent = new Map<string | null, ScopeGroup[]>();
  for (const group of groups) {
    const key = group.parentId ?? null;
    const siblings = byParent.get(key);
    if (siblings) {
      siblings.push(group);
    } else {
      byParent.set(key, [group]);
    }
  }
  return byParent;
}

interface ScopeTreeNodeProps {
  group: ScopeGroup;
  childrenByParent: Map<string | null, ScopeGroup[]>;
  labelById: Map<string, string>;
  selectedId: string | null;
  onSelectNode: (id: string) => void;
}

function ScopeTreeNode({
  group,
  childrenByParent,
  labelById,
  selectedId,
  onSelectNode,
}: ScopeTreeNodeProps) {
  const [open, setOpen] = useState(false);
  const childGroups = childrenByParent.get(group.id) ?? [];
  const hasChildren = childGroups.length > 0 || group.memberIds.length > 0;

  return (
    <li className="scope-tree__node">
      <div className="scope-tree__row">
        {hasChildren ? (
          <button
            type="button"
            className="scope-tree__toggle"
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} ${group.label}`}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "−" : "+"}
          </button>
        ) : (
          <span className="scope-tree__toggle scope-tree__toggle--spacer" />
        )}
        <span className="scope-tree__group-label">{group.label}</span>
      </div>
      {open ? (
        <>
          {group.memberIds.length > 0 ? (
            <ul className="scope-tree__members">
              {group.memberIds.map((memberId) => {
                const label = labelById.get(memberId) ?? memberId;
                return (
                  <li key={memberId}>
                    <button
                      type="button"
                      className={
                        selectedId === memberId
                          ? "scope-tree__member scope-tree__member--active"
                          : "scope-tree__member"
                      }
                      title={label}
                      onClick={() => onSelectNode(memberId)}
                    >
                      {label}
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : null}
          {childGroups.length > 0 ? (
            <ul className="scope-tree__children">
              {childGroups.map((child) => (
                <ScopeTreeNode
                  key={child.id}
                  group={child}
                  childrenByParent={childrenByParent}
                  labelById={labelById}
                  selectedId={selectedId}
                  onSelectNode={onSelectNode}
                />
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </li>
  );
}

export function ScopeTree({
  scopeGroups,
  nodes,
  selectedId,
  onSelectNode,
}: ScopeTreeProps) {
  const childrenByParent = useMemo(
    () => buildChildrenByParent(scopeGroups ?? []),
    [scopeGroups],
  );
  const roots = childrenByParent.get(null) ?? [];
  const labelById = useMemo(
    () => new Map((nodes ?? []).map((node) => [node.id, node.label])),
    [nodes],
  );

  if (!scopeGroups || scopeGroups.length === 0) {
    return (
      <section className="scope-tree" aria-label="Scope tree">
        <p className="scope-tree__label">Scope tree</p>
        <p className="muted">No scope groups in this graph snapshot.</p>
      </section>
    );
  }

  return (
    <section className="scope-tree" aria-label="Scope tree">
      <p className="scope-tree__label">Scope tree</p>
      <ul className="scope-tree__roots">
        {roots.map((group) => (
          <ScopeTreeNode
            key={group.id}
            group={group}
            childrenByParent={childrenByParent}
            labelById={labelById}
            selectedId={selectedId}
            onSelectNode={onSelectNode}
          />
        ))}
      </ul>
    </section>
  );
}
