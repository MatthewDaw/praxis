const GITHUB_REPO_URL = "https://github.com/Antonelli-Tech-Solutions/praxis";

export function GitHubRepoLink() {
  return (
    <a
      className="github-repo-link"
      href={GITHUB_REPO_URL}
      target="_blank"
      rel="noreferrer noopener"
      aria-label="View PRAXIS repository on GitHub"
      title="PRAXIS on GitHub"
    >
      <svg
        className="github-repo-link__icon"
        viewBox="0 0 24 24"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
        focusable="false"
      >
        <path
          fill="currentColor"
          d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.727-4.042-1.61-4.042-1.61-.546-1.385-1.333-1.755-1.333-1.755-1.087-.745.083-.73.083-.73 1.205.085 1.838 1.238 1.838 1.238 1.07 1.835 2.807 1.305 3.492.997.108-.775.42-1.305.763-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.467-2.382 1.235-3.222-.123-.303-.535-1.523.117-3.177 0 0 1.008-.322 3.3 1.23a11.48 11.48 0 0 1 3.003-.405c1.018.005 2.043.138 3.003.405 2.292-1.552 3.297-1.23 3.297-1.23.655 1.654.243 2.874.12 3.177.77.84 1.233 1.912 1.233 3.222 0 4.61-2.807 5.625-5.48 5.92.432.372.817 1.103.817 2.222 0 1.605-.015 2.898-.015 3.292 0 .322.217.697.825.577C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"
        />
      </svg>
    </a>
  );
}
