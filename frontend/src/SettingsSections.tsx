import { useState } from "react";

import AiAccountsPage from "./AiAccountsPage";
import CodingAgentsPage from "./CodingAgentsPage";

type SectionId = "coding-agents" | "ai-accounts";

const TABS: Array<{ id: SectionId; label: string; detail: string }> = [
  { id: "coding-agents", label: "Coding Agents", detail: "OpenCode CLI and Qoder CLI" },
  { id: "ai-accounts", label: "AI Accounts", detail: "Browser provider accounts for the council" }
];

export default function SettingsSections({
  section,
  onSection
}: {
  section?: SectionId;
  onSection?: (next: SectionId) => void;
}) {
  const [inner, setInner] = useState<SectionId>("coding-agents");
  const tab = section ?? inner;
  const setTab = (next: SectionId) => {
    if (onSection) onSection(next);
    else setInner(next);
  };
  return (
    <div className="settings-sections">
      <div className="subnav" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.id}
            role="tab"
            aria-selected={tab === item.id}
            className={`subnav-tab${tab === item.id ? " active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            <strong>{item.label}</strong>
            <small>{item.detail}</small>
          </button>
        ))}
      </div>
      {tab === "coding-agents" ? <CodingAgentsPage /> : <AiAccountsPage />}
    </div>
  );
}
