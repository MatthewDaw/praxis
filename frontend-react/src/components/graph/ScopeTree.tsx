import { useMemo, useState } from "react";
import type { GraphNode, ScopeGroup } from "../../types/graph";

interface ScopeTreeProps {
  scopeGroups?: ScopeGroup[];
  /** Graph nodes, used to resolve member ids to their human-readable labels. */
  nodes?: GraphNode[];
  selectedId: string | null;
  onSelectNode: (id: string) => void;
}

function buildTree(groups: ScopeGroup[]): ScopeGroup[] {
  const roots = groups.filter((g) => g.parentId == null);
  return roots;
}

function childrenOf(parentId: string, groups: ScopeGroup[]): ScopeGroup[] {
  return groups.filter((g) => g.parentId === parentId);
}

interface ScopeTreeNodeProps {
  group: ScopeGroup;
  allGroups: ScopeGroup[];
  labelById: Map<string, string>;
  selectedId: string | null;
  onSelectNode: (id: string) => void;
  depth: number;
}

function ScopeTreeNode({
  group,
  allGroups,
  labelById,
  selectedId,
  onSelectNode,
  depth,
}: ScopeTreeNodeProps) {
  const [open, setOpen] = useState(false);
  const childGroups = childrenOf(group.id, allGroups);
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
                  allGroups={allGroups}
                  labelById={labelById}
                  selectedId={selectedId}
                  onSelectNode={onSelectNode}
                  depth={depth + 1}
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
  const roots = useMemo(
    () => (scopeGroups ? buildTree(scopeGroups) : []),
    [scopeGroups],
  );
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
            allGroups={scopeGroups}
            labelById={labelById}
            selectedId={selectedId}
            onSelectNode={onSelectNode}
            depth={0}
          />
        ))}
      </ul>
    </section>
  );
}
