import type { ReactNode } from "react";

interface ContentSplitProps {
  list: ReactNode;
  detail: ReactNode;
}

export function ContentSplit({ list, detail }: ContentSplitProps) {
  return (
    <div className="content-split">
      <div className="content-split__list">{list}</div>
      <div className="content-split__detail detail-panel--sticky">{detail}</div>
    </div>
  );
}
