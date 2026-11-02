import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // The fallback is the only user-visible place; keep the real stack in the
    // console so a crash is diagnosable without a blank page.
    console.error("Dashboard render failed:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="boot-screen">
          <div className="mesh-mark large" aria-hidden>
            <i />
            <i />
            <i />
            <i />
          </div>
          <h1>FORGEMESH</h1>
          <p>The dashboard hit a rendering error. The server is still running.</p>
          <pre className="error-box">{this.state.error.message}</pre>
          <button
            className="btn"
            onClick={() => {
              this.setState({ error: null });
              window.location.reload();
            }}
          >
            Reload dashboard
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root is missing from index.html");
}

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
