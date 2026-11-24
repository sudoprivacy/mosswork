import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  connectorsApi,
  type ConnectorCatalogEntry,
  type ConnectorInstance,
} from "../../../api/modules/connectors";
import { SlackDrawer } from "./SlackDrawer";

vi.mock("react-i18next", () => {
  const translate = (key: string) => key;
  return {
    useTranslation: () => ({ t: translate }),
    initReactI18next: { type: "3rdParty", init: () => {} },
  };
});

const entry: ConnectorCatalogEntry = {
  kind: "slack",
  name: "Slack",
  description: "",
  auth_kind: "oauth2",
  doc_url: "",
  icon: "slack",
  color: "",
  phase: "available",
  mcp_mode: "remote",
  category: "office",
  redirect_uri: "https://work.example/api/connectors/oauth/callback",
  mcp_url: "https://mcp.slack.com/mcp",
};
const instance: ConnectorInstance = {
  kind: "slack",
  instance_id: "slack-one",
  display_name: "My Slack",
  status: "active",
  mcp_server_name: "slack__one",
  has_credentials: true,
  shared: false,
  owner_user_id: 1,
  can_manage: true,
  created_at: 0,
  updated_at: 0,
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("shows the setup guide when creating a Slack connector", () => {
  render(
    <SlackDrawer
      entry={entry}
      instance={null}
      onClose={vi.fn()}
      onSaved={vi.fn()}
    />,
  );
  expect(
    screen.getByRole("button", { name: /connectors\.slack\.guide\.title/ }),
  ).toHaveAttribute("aria-expanded", "true");
  expect(
    screen.getByRole("link", { name: "connectors.slack.guide.apps" }),
  ).toHaveAttribute("href", "https://api.slack.com/apps");
  expect(screen.getByText("connectors.slack.guide.scopes")).toBeVisible();
});

it("opens authorization for the saved instance and receives status across browser origins", async () => {
  vi.useFakeTimers();
  vi.spyOn(connectorsApi, "getInstance").mockResolvedValue({
    ...instance,
    config: {},
    credentials_preview: {
      client_id: "client-one",
      client_secret_configured: true,
    },
  });
  const start = vi.spyOn(connectorsApi, "oauthStart").mockResolvedValue({
    authorize_url: "https://slack.com/oauth/v2_user/authorize",
    state_id: "state-one",
  });
  vi.spyOn(window, "open").mockReturnValue(null);
  const pending = vi
    .spyOn(connectorsApi, "oauthPending")
    .mockResolvedValue({ kind: "slack", applied: true, tokens: {} });
  await act(async () => {
    render(
      <SlackDrawer
        entry={entry}
        instance={instance}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );
  });
  expect(screen.getByLabelText("Client Secret")).toHaveValue("");
  await act(async () => {
    fireEvent.click(
      screen.getByRole("button", { name: "connectors.goToAuthorize" }),
    );
  });
  expect(start).toHaveBeenCalledWith(
    { type: "catalog", kind: "slack", instance_id: "slack-one" },
    "/connectors",
  );
  expect(
    screen.getByRole("link", { name: "connectors.openAuthorizePage" }),
  ).toHaveAttribute("target", "_blank");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2500);
  });
  expect(pending).toHaveBeenCalledWith("state-one");
  expect(
    screen.getByRole("button", { name: "connectors.probe" }),
  ).toBeEnabled();
});
