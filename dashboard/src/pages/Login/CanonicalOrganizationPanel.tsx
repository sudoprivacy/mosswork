import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import type { CanonicalIdentity } from "../../api/modules/canonicalIdentity";
import type {
  CanonicalDepartment,
  CanonicalInvitation,
  CanonicalOrganizationMember,
  DepartmentChanges,
} from "../../api/modules/canonicalOrganization";
import {
  showsFullOrganizationAdmin,
  useCanonicalOrganization,
} from "../../hooks/useCanonicalOrganization";
import { parseApiError } from "../../utils/apiError";

type Editor =
  | { readonly kind: "create" }
  | { readonly kind: "department"; readonly row: CanonicalDepartment }
  | { readonly kind: "password"; readonly row: CanonicalOrganizationMember }
  | { readonly kind: "add" };

export function CanonicalOrganizationPanel({
  identity,
}: {
  readonly identity: CanonicalIdentity;
}) {
  const { t } = useTranslation();
  const state = useCanonicalOrganization(identity);
  const fullAdmin = showsFullOrganizationAdmin(identity.member.role);
  const [tab, setTab] = useState("members");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [name, setName] = useState("");
  const [parentId, setParentId] = useState<string | null>(null);
  const [externalId, setExternalId] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState(false);
  const [invitePage, setInvitePage] = useState(1);
  const [inviteStatus, setInviteStatus] = useState<
    CanonicalInvitation["status"] | undefined
  >();
  const disabled = state.isPending || state.isLoading || !state.data;
  const directWriteUnconfirmed =
    state.error instanceof Error &&
    state.error.message === "canonicalOrganization.writeUnconfirmed";

  useEffect(() => {
    setEditor(null);
    setPassword("");
    setPhone("");
  }, [state.data]);

  function errorMessage(error: unknown) {
    const local =
      error instanceof Error &&
      (error.message === "canonicalOrganization.readUnavailable" ||
        error.message === "canonicalOrganization.writeUnconfirmed")
        ? error.message
        : "";
    if (local) return t(local);
    const code = parseApiError(error)?.code;
    const key = code ? `apiErrors.${code}` : "canonicalOrganization.error";
    return t(key) === key ? t("canonicalOrganization.error") : t(key);
  }
  function parentLabel(id: string | null) {
    if (id === null) return t("canonicalOrganization.root");
    const rows = new Map(
      state.data?.departments.map((row) => [row.department_id, row]) ?? [],
    );
    const names: string[] = [];
    const seen = new Set<string>();
    let row = rows.get(id);
    while (row && !seen.has(row.department_id)) {
      seen.add(row.department_id);
      names.unshift(row.name);
      row = row.parent_id === null ? undefined : rows.get(row.parent_id);
    }
    return names.length
      ? names.join(" / ")
      : t("canonicalOrganization.unlistedDepartment");
  }
  function displayRole(role: string) {
    const labels: Record<string, string> = {
      admin: t("canonicalOrganization.roleAdmin"),
      super_admin: t("canonicalOrganization.roleSuperAdmin"),
      dept_admin: t("canonicalOrganization.roleDepartmentAdmin"),
      user: t("canonicalOrganization.roleMember"),
    };
    return labels[role] ?? role;
  }

  function open(value: Editor) {
    setNotice(false);
    setEditor(value);
    setName(value.kind === "department" ? value.row.name : "");
    const actorDepartment = state.data?.members.find(
      (row) => row.member_id === identity.member.id,
    )?.department_id;
    setParentId(
      value.kind === "department"
        ? value.row.parent_id
        : fullAdmin
        ? null
        : actorDepartment ?? null,
    );
    setExternalId("");
    setPhone("");
    setPassword("");
  }
  async function save() {
    if (!editor || disabled) return;
    setNotice(false);
    let ok = false;
    try {
      switch (editor.kind) {
        case "create":
          ok = await state.createDepartment({
            name,
            parent_id: parentId,
            ...(externalId ? { external_id: externalId } : {}),
          });
          break;
        case "department": {
          const changes: DepartmentChanges = {
            ...(name !== editor.row.name ? { name } : {}),
            ...(parentId !== editor.row.parent_id
              ? { parent_id: parentId }
              : {}),
          };
          if (!Object.keys(changes).length) {
            setEditor(null);
            return;
          }
          ok = await state.updateDepartment(editor.row.department_id, changes);
          break;
        }
        case "password":
          ok = await state.setMemberPassword(editor.row.member_id, password);
          break;
        case "add":
          ok = await state.addRegisteredMember(phone.trim());
          break;
      }
      if (ok) {
        setEditor(null);
        setNotice(true);
      }
    } finally {
      setPassword("");
    }
  }
  const readInvites = (
    page: number,
    status: CanonicalInvitation["status"] | undefined,
  ) => {
    setInvitePage(page);
    return state.loadInvitations({
      limit: 20,
      offset: (page - 1) * 20,
      ...(status ? { status } : {}),
    });
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Title level={4}>
        {t("canonicalOrganization.title")}
      </Typography.Title>
      {state.error != null &&
        !(state.writeUnconfirmed && directWriteUnconfirmed) && (
          <Alert showIcon type="error" message={errorMessage(state.error)} />
        )}
      {state.writeUnconfirmed && (
        <Alert
          type="warning"
          showIcon
          message={t("canonicalOrganization.writeUnconfirmed")}
        />
      )}
      {notice && (
        <Alert type="success" message={t("canonicalOrganization.saved")} />
      )}
      <Button
        disabled={state.isPending}
        onClick={() => {
          setNotice(false);
          void state.reload();
        }}
      >
        {t("canonicalOrganization.reload")}
      </Button>
      <Tabs
        activeKey={tab}
        onChange={(key) => {
          setTab(key);
          if (key === "invitations") void readInvites(invitePage, inviteStatus);
        }}
        items={[
          {
            key: "members",
            label: t("canonicalOrganization.members"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }}>
                {fullAdmin && (
                  <Button
                    disabled={disabled}
                    onClick={() => open({ kind: "add" })}
                  >
                    {t("canonicalOrganization.addExistingAccount")}
                  </Button>
                )}
                <Table
                  rowKey="member_id"
                  dataSource={state.data?.members ?? []}
                  loading={state.isLoading || state.isPending}
                  scroll={{ x: 720 }}
                  pagination={{ pageSize: 10 }}
                  columns={[
                    {
                      title: t("canonicalOrganization.member"),
                      key: "name",
                      render: (_, row: CanonicalOrganizationMember) => (
                        <Space direction="vertical" size={0}>
                          <Typography.Text>
                            {row.display_name || row.username}
                          </Typography.Text>
                          <Typography.Text type="secondary">
                            {row.username}
                          </Typography.Text>
                        </Space>
                      ),
                    },
                    {
                      title: t("canonicalOrganization.role"),
                      dataIndex: "role",
                      render: (role: string) => displayRole(role),
                    },
                    {
                      title: t("canonicalOrganization.status"),
                      dataIndex: "status",
                      render: (status: string) => (
                        <Tag>
                          {status === "active"
                            ? t("canonicalOrganization.active")
                            : status === "disabled"
                            ? t("canonicalOrganization.disabled")
                            : status}
                        </Tag>
                      ),
                    },
                    {
                      title: t("canonicalOrganization.department"),
                      dataIndex: "department_id",
                      render: (id: string | null) =>
                        id === null
                          ? t("canonicalOrganization.noDepartment")
                          : parentLabel(id),
                    },
                    {
                      title: t("canonicalOrganization.actions"),
                      key: "actions",
                      render: (_, row: CanonicalOrganizationMember) => (
                        <Button
                          disabled={disabled}
                          onClick={() => open({ kind: "password", row })}
                        >
                          {t("canonicalOrganization.setPassword")}
                        </Button>
                      ),
                    },
                  ]}
                />
              </Space>
            ),
          },
          {
            key: "departments",
            label: t("canonicalOrganization.departments"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Button
                  disabled={
                    disabled || (!fullAdmin && !state.data?.departments.length)
                  }
                  onClick={() => open({ kind: "create" })}
                >
                  {t("canonicalOrganization.createDepartment")}
                </Button>
                <Table
                  rowKey="department_id"
                  dataSource={state.data?.departments ?? []}
                  loading={state.isLoading || state.isPending}
                  scroll={{ x: 720 }}
                  pagination={{ pageSize: 10 }}
                  columns={[
                    {
                      title: t("canonicalOrganization.departmentName"),
                      dataIndex: "name",
                    },
                    {
                      title: t("canonicalOrganization.parent"),
                      dataIndex: "parent_id",
                      render: (id: string | null) => parentLabel(id),
                    },
                    {
                      title: t("canonicalOrganization.memberCount"),
                      dataIndex: "user_count",
                    },
                    {
                      title: t("canonicalOrganization.actions"),
                      key: "actions",
                      render: (_, row: CanonicalDepartment) => (
                        <Space>
                          <Button
                            disabled={disabled}
                            onClick={() => open({ kind: "department", row })}
                          >
                            {t("canonicalOrganization.edit")}
                          </Button>
                          <Popconfirm
                            title={t("canonicalOrganization.confirmDelete")}
                            disabled={disabled}
                            onConfirm={async () => {
                              setNotice(false);
                              if (
                                await state.deleteDepartment(row.department_id)
                              )
                                setNotice(true);
                            }}
                          >
                            <Button
                              danger
                              disabled={
                                disabled ||
                                row.user_count > 0 ||
                                !!state.data?.departments.some(
                                  (child) =>
                                    child.parent_id === row.department_id,
                                )
                              }
                            >
                              {t("canonicalOrganization.delete")}
                            </Button>
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
              </Space>
            ),
          },
          ...(fullAdmin
            ? [
                {
                  key: "invitations",
                  label: t("canonicalOrganization.invitations"),
                  children: (
                    <Space direction="vertical" style={{ width: "100%" }}>
                      {state.inviteError != null && (
                        <Alert
                          type="error"
                          message={errorMessage(state.inviteError)}
                        />
                      )}
                      <Space wrap>
                        <Select
                          popupMatchSelectWidth={180}
                          aria-label={t(
                            "canonicalOrganization.invitationStatus",
                          )}
                          value={inviteStatus ?? "all"}
                          disabled={disabled || state.invitesLoading}
                          options={[
                            {
                              value: "all",
                              label: t("canonicalOrganization.all"),
                            },
                            ...(["pending", "used", "revoked"] as const).map(
                              (value) => ({
                                value,
                                label: t(`canonicalOrganization.${value}`),
                              }),
                            ),
                          ]}
                          onChange={(value) => {
                            const selected =
                              value === "all"
                                ? undefined
                                : (value as CanonicalInvitation["status"]);
                            setInviteStatus(selected);
                            void readInvites(1, selected);
                          }}
                        />
                        <Button
                          disabled={disabled || state.invitesLoading}
                          onClick={() =>
                            void readInvites(invitePage, inviteStatus)
                          }
                        >
                          {t("canonicalOrganization.reloadInvitations")}
                        </Button>
                      </Space>
                      <Table
                        rowKey="invitation_id"
                        dataSource={state.invitations?.items ?? []}
                        loading={state.invitesLoading}
                        scroll={{ x: 600 }}
                        pagination={{
                          current: invitePage,
                          pageSize: 20,
                          total: state.invitations?.total ?? 0,
                          showSizeChanger: false,
                          onChange: (page) =>
                            void readInvites(page, inviteStatus),
                        }}
                        columns={[
                          {
                            title: t("canonicalOrganization.invitationCode"),
                            dataIndex: "code",
                            render: (value: string) => (
                              <Typography.Text code copyable={{ text: value }}>
                                {value}
                              </Typography.Text>
                            ),
                          },
                          {
                            title: t("canonicalOrganization.status"),
                            dataIndex: "status",
                            render: (status: CanonicalInvitation["status"]) =>
                              t(`canonicalOrganization.${status}`),
                          },
                          {
                            title: t("canonicalOrganization.initialCredit"),
                            dataIndex: "initial_credit_units",
                            render: (value: number) => String(value),
                          },
                        ]}
                      />
                    </Space>
                  ),
                },
              ]
            : []),
        ]}
      />
      <Modal
        open={editor !== null}
        title={
          editor?.kind === "create"
            ? t("canonicalOrganization.createDepartment")
            : editor?.kind === "department"
            ? t("canonicalOrganization.editDepartment")
            : editor?.kind === "add"
            ? t("canonicalOrganization.addExistingAccount")
            : t("canonicalOrganization.setPassword")
        }
        confirmLoading={state.isPending}
        okText={t("canonicalOrganization.save")}
        cancelText={t("canonicalOrganization.cancel")}
        onCancel={() => {
          if (!state.isPending) {
            setEditor(null);
            setPassword("");
            setPhone("");
          }
        }}
        onOk={() => void save()}
        okButtonProps={{
          disabled:
            disabled ||
            (editor?.kind === "password"
              ? password.length === 0
              : editor?.kind === "add"
              ? !phone.trim()
              : !name.trim()),
        }}
      >
        <Form layout="vertical">
          {(editor?.kind === "create" || editor?.kind === "department") && (
            <>
              <Form.Item label={t("canonicalOrganization.departmentName")}>
                <Input
                  aria-label={t("canonicalOrganization.departmentName")}
                  value={name}
                  disabled={state.isPending}
                  onChange={(event) => setName(event.target.value)}
                />
              </Form.Item>
              <Form.Item label={t("canonicalOrganization.parent")}>
                <Select
                  aria-label={t("canonicalOrganization.parent")}
                  value={parentId ?? undefined}
                  placeholder={t("canonicalOrganization.root")}
                  disabled={state.isPending}
                  allowClear={fullAdmin}
                  options={[
                    ...(parentId !== null &&
                    !state.data?.departments.some(
                      (row) => row.department_id === parentId,
                    )
                      ? [
                          {
                            value: parentId,
                            label: t(
                              "canonicalOrganization.unlistedDepartment",
                            ),
                            disabled: true,
                          },
                        ]
                      : []),
                    ...(state.data?.departments ?? [])
                      .filter(
                        (row) =>
                          editor.kind !== "department" ||
                          row.department_id !== editor.row.department_id,
                      )
                      .map((row) => ({
                        value: row.department_id,
                        label: parentLabel(row.department_id),
                      })),
                  ]}
                  onChange={(id: string | undefined) => setParentId(id ?? null)}
                />
              </Form.Item>
              {editor.kind === "create" && (
                <Form.Item label={t("canonicalOrganization.externalId")}>
                  <Input
                    aria-label={t("canonicalOrganization.externalId")}
                    value={externalId}
                    disabled={state.isPending}
                    onChange={(event) => setExternalId(event.target.value)}
                  />
                </Form.Item>
              )}
            </>
          )}
          {editor?.kind === "password" && (
            <>
              <Typography.Paragraph>
                {editor.row.display_name || editor.row.username}
              </Typography.Paragraph>
              <Form.Item label={t("canonicalOrganization.newPassword")}>
                <Input.Password
                  aria-label={t("canonicalOrganization.newPassword")}
                  autoComplete="new-password"
                  value={password}
                  disabled={state.isPending}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </Form.Item>
            </>
          )}
          {editor?.kind === "add" && (
            <Form.Item label={t("canonicalOrganization.existingAccountPhone")}>
              <Input
                aria-label={t("canonicalOrganization.existingAccountPhone")}
                value={phone}
                disabled={state.isPending}
                onChange={(event) => setPhone(event.target.value)}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </Space>
  );
}
