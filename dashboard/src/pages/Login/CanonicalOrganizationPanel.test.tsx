import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CanonicalMemberLogin } from "./CanonicalLogin";
import {
  canonicalIdentityApi,
  type CanonicalIdentity,
} from "../../api/modules/canonicalIdentity";
import { canonicalOrganizationApi } from "../../api/modules/canonicalOrganization";
import {
  clearSetupRequired,
  getAuthToken,
  setAuthTokens,
} from "../../api/request";
import en from "../../locales/en.json";

vi.mock("react-i18next", async () => {
  const { default: catalog } = await import("../../locales/en.json");
  return {
    useTranslation: () => ({
      t: (key: string) => {
        const [namespace, name] = key.split(".");
        if (namespace === "canonicalOrganization")
          return (
            catalog.canonicalOrganization[
              name as keyof typeof catalog.canonicalOrganization
            ] ?? key
          );
        if (namespace === "canonical")
          return (
            catalog.canonical[name as keyof typeof catalog.canonical] ?? key
          );
        return key;
      },
    }),
  };
});
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../../i18n", () => ({ default: { language: "en" } }));

const identity: CanonicalIdentity = {
  account_id: "original-account",
  organization: { id: "original-org", name: "Company" },
  member: {
    id: "original-actor",
    role: "admin",
    status: "active",
    revision: 1,
  },
  moss_access: { is_enabled: false, is_granted: false, source: "explicit" },
  personal_agent_ref: null,
  can_switch_organization: false,
};
const actor = {
  account_id: identity.account_id,
  organization_id: identity.organization.id,
  member_id: identity.member.id,
  username: "original-admin",
  display_name: "Current administrator",
  role: "admin",
  status: "active",
  membership_revision: 1,
  created_at: 1234,
  legacy_id: null,
  department_id: null,
};
const retained = {
  ...actor,
  account_id: null,
  member_id: "retained-member",
  username: "retained-user",
  display_name: "Retained disabled member",
  role: "user",
  status: "disabled",
  legacy_id: -42,
};
const department = {
  department_id: "original-department",
  organization_id: identity.organization.id,
  name: "Original department",
  parent_id: null,
  created_at: 1234,
  updated_at: 1235,
  user_count: 0,
};
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  clearSetupRequired();
  setAuthTokens("original-access", "original-refresh", false);
  vi.spyOn(canonicalIdentityApi, "me").mockResolvedValue(identity);
  vi.spyOn(canonicalOrganizationApi, "members").mockResolvedValue([
    actor,
    retained,
  ]);
  vi.spyOn(canonicalOrganizationApi, "departments").mockResolvedValue([
    department,
  ]);
  vi.spyOn(canonicalOrganizationApi, "invitations").mockResolvedValue({
    items: [],
    total: 0,
  });
});
afterEach(() => vi.restoreAllMocks());

async function openDirectory() {
  render(
    <CanonicalMemberLogin phoneLoginAvailable={false} passwordLoginAvailable />,
  );
  await screen.findByText(en.canonical.runtimePending);
  fireEvent.click(
    screen.getByRole("button", { name: en.canonicalOrganization.open }),
  );
  await screen.findByText(retained.display_name);
}

it("loads the current directory only after an explicit account-screen action", async () => {
  render(
    <CanonicalMemberLogin phoneLoginAvailable={false} passwordLoginAvailable />,
  );
  await screen.findByText(en.canonical.runtimePending);
  expect(canonicalOrganizationApi.members).not.toHaveBeenCalled();
  expect(canonicalOrganizationApi.invitations).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: en.canonicalOrganization.open }),
  );
  await screen.findByText(retained.display_name);
  expect(
    screen.getByText(en.canonicalOrganization.disabled),
  ).toBeInTheDocument();
  expect(
    screen.getAllByText(en.canonicalOrganization.noDepartment),
  ).toHaveLength(2);
  expect(canonicalOrganizationApi.members).toHaveBeenCalledWith(
    "original-access",
    expect.any(AbortSignal),
  );
  expect(canonicalOrganizationApi.invitations).not.toHaveBeenCalled();
});

it("withholds a possible department-write outcome and reloads the original records without replay", async () => {
  let effects = 0;
  const committed = {
    ...department,
    department_id: "retained-committed-department",
    name: "Committed department",
  };
  const create = vi
    .spyOn(canonicalOrganizationApi, "createDepartment")
    .mockImplementation(async () => {
      effects++;
      vi.mocked(canonicalOrganizationApi.departments).mockResolvedValue([
        department,
        committed,
      ]);
      throw new Error("Request failed: 503");
    });
  await openDirectory();
  fireEvent.click(
    screen.getByRole("tab", { name: en.canonicalOrganization.departments }),
  );
  fireEvent.click(
    screen.getByRole("button", {
      name: en.canonicalOrganization.createDepartment,
    }),
  );
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(
    within(dialog).getByLabelText(en.canonicalOrganization.departmentName),
    { target: { value: "Committed department" } },
  );
  fireEvent.click(
    within(dialog).getByRole("button", { name: en.canonicalOrganization.save }),
  );
  await screen.findByText(en.canonicalOrganization.writeUnconfirmed);
  expect(
    screen.queryByText(en.canonicalOrganization.saved),
  ).not.toBeInTheDocument();
  expect(effects).toBe(1);
  expect(create).toHaveBeenCalledWith(
    { name: "Committed department", parent_id: null },
    "original-access",
  );
  fireEvent.click(
    screen.getByRole("button", { name: en.canonicalOrganization.reload }),
  );
  await screen.findByText(committed.name);
  expect(create).toHaveBeenCalledTimes(1);
  expect(canonicalOrganizationApi.departments).toHaveBeenCalledTimes(2);
});

it("clears the invitation filter instead of silently reusing the previous status", async () => {
  await openDirectory();
  fireEvent.click(
    screen.getByRole("tab", { name: en.canonicalOrganization.invitations }),
  );
  await waitFor(() =>
    expect(canonicalOrganizationApi.invitations).toHaveBeenCalledTimes(1),
  );
  const status = screen.getByRole("combobox", {
    name: en.canonicalOrganization.invitationStatus,
  });
  const chooseStatus = async (label: string) => {
    await waitFor(() => expect(status).toBeEnabled());
    if (status.getAttribute("aria-expanded") !== "true")
      fireEvent.mouseDown(status);
    // The virtual list exposes only adjacent rows through role=option.
    // Click the actual visible option's label, including off-cursor rows.
    fireEvent.click(await screen.findByTitle(label));
  };
  await chooseStatus(en.canonicalOrganization.used);
  await waitFor(() =>
    expect(canonicalOrganizationApi.invitations).toHaveBeenCalledTimes(2),
  );
  expect(
    vi.mocked(canonicalOrganizationApi.invitations).mock.calls[1][0],
  ).toEqual({ limit: 20, offset: 0, status: "used" });
  await chooseStatus(en.canonicalOrganization.all);
  await waitFor(() =>
    expect(canonicalOrganizationApi.invitations).toHaveBeenCalledTimes(3),
  );
  expect(
    vi.mocked(canonicalOrganizationApi.invitations).mock.calls[2][0],
  ).toEqual({ limit: 20, offset: 0 });
});

it("keeps invitation and account-add controls outside the department-administrator display ceiling", async () => {
  vi.mocked(canonicalIdentityApi.me).mockResolvedValue({
    ...identity,
    member: { ...identity.member, role: "dept_admin" },
  });
  vi.mocked(canonicalOrganizationApi.members).mockResolvedValue([
    { ...actor, role: "dept_admin", department_id: department.department_id },
    { ...retained, department_id: department.department_id },
  ]);
  await openDirectory();
  expect(
    screen.queryByRole("tab", { name: en.canonicalOrganization.invitations }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", {
      name: en.canonicalOrganization.addExistingAccount,
    }),
  ).not.toBeInTheDocument();
  expect(canonicalOrganizationApi.invitations).not.toHaveBeenCalled();
});

it("sets a retained member password through the original member port without trimming or replacing the actor", async () => {
  const write = vi
    .spyOn(canonicalOrganizationApi, "setMemberPassword")
    .mockResolvedValue();
  await openDirectory();
  const row = screen.getByText(retained.display_name).closest("tr");
  expect(row).not.toBeNull();
  fireEvent.click(
    within(row!).getByRole("button", {
      name: en.canonicalOrganization.setPassword,
    }),
  );
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(
    within(dialog).getByLabelText(en.canonicalOrganization.newPassword),
    { target: { value: "  explicit password  " } },
  );
  fireEvent.click(
    within(dialog).getByRole("button", { name: en.canonicalOrganization.save }),
  );
  await screen.findByText(en.canonicalOrganization.saved);
  expect(write).toHaveBeenCalledExactlyOnceWith(
    retained.member_id,
    "  explicit password  ",
    "original-access",
  );
  expect(canonicalOrganizationApi.members).toHaveBeenCalledTimes(2);
  expect(getAuthToken()).toBe("original-access");
});
