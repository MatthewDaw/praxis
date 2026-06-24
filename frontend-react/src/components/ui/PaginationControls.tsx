interface PaginationControlsProps {
  page: number;
  pageSize: number;
  totalItems: number;
  pageSizeOptions?: number[];
  pageSizeLabel?: string;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
}

const DEFAULT_PAGE_SIZE_OPTIONS = [10, 25, 50];

function pageWindow(currentPage: number, totalPages: number): number[] {
  const start = Math.max(1, Math.min(currentPage - 2, totalPages - 4));
  const end = Math.min(totalPages, start + 4);
  return Array.from({ length: end - start + 1 }, (_, index) => start + index);
}

export function PaginationControls({
  page,
  pageSize,
  totalItems,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
  pageSizeLabel = "Items per page",
  onPageChange,
  onPageSizeChange,
}: PaginationControlsProps) {
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
  const normalizedPage = Math.min(Math.max(page, 1), totalPages);
  const start = totalItems === 0 ? 0 : (normalizedPage - 1) * pageSize + 1;
  const end = Math.min(totalItems, normalizedPage * pageSize);

  return (
    <nav
      className="pagination-controls"
      aria-label="Candidate pagination"
    >
      <p className="pagination-controls__summary">
        Showing {start}-{end} of {totalItems}
      </p>
      <div className="pagination-controls__buttons">
        <button
          type="button"
          className="btn secondary"
          disabled={normalizedPage === 1}
          onClick={() => onPageChange(1)}
        >
          First
        </button>
        <button
          type="button"
          className="btn secondary"
          disabled={normalizedPage === 1}
          onClick={() => onPageChange(normalizedPage - 1)}
        >
          Previous
        </button>
        {pageWindow(normalizedPage, totalPages).map((pageNumber) => (
          <button
            key={pageNumber}
            type="button"
            className={
              pageNumber === normalizedPage
                ? "btn primary pagination-controls__page"
                : "btn pagination-controls__page"
            }
            aria-current={pageNumber === normalizedPage ? "page" : undefined}
            onClick={() => onPageChange(pageNumber)}
          >
            {pageNumber}
          </button>
        ))}
        <button
          type="button"
          className="btn secondary"
          disabled={normalizedPage === totalPages}
          onClick={() => onPageChange(normalizedPage + 1)}
        >
          Next
        </button>
        <button
          type="button"
          className="btn secondary"
          disabled={normalizedPage === totalPages}
          onClick={() => onPageChange(totalPages)}
        >
          Last
        </button>
      </div>
      <label className="pagination-controls__size">
        {pageSizeLabel}
        <select
          value={pageSize}
          onChange={(event) => onPageSizeChange(Number(event.target.value))}
        >
          {pageSizeOptions.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>
    </nav>
  );
}
