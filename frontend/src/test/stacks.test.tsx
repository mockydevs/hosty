import { type JsonSchema, SchemaForm, schemaFields } from "@/components/schema-form";
import { blueprintDisplayName } from "@/pages/stack-create";
/** Stacks (v2/M4): the schema-driven form renderer — blueprint inputs JSON
 * Schema → fields, validation, value coercion, secret handling. */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

/** Trimmed-down pydantic model_json_schema() of RawImageInputs. */
const RAW_IMAGE_SCHEMA: JsonSchema = {
  type: "object",
  title: "RawImageInputs",
  required: ["image", "internal_port"],
  properties: {
    image: {
      type: "string",
      title: "Image",
      description: "OCI image reference, e.g. ghcr.io/acme/app:v1",
      maxLength: 512,
    },
    internal_port: {
      type: "integer",
      title: "Internal Port",
      minimum: 1,
      maximum: 65535,
    },
    domain: { anyOf: [{ type: "string", maxLength: 253 }, { type: "null" }], default: null },
    behind_cloudflare: { type: "boolean", default: false, title: "Behind Cloudflare" },
    env: {
      type: "object",
      additionalProperties: { type: "string" },
      title: "Env",
      default: {},
    },
    memory_mb: {
      anyOf: [{ type: "integer", minimum: 16, maximum: 1048576 }, { type: "null" }],
      default: null,
      title: "Memory Mb",
    },
  },
};

describe("schemaFields", () => {
  it("maps every supported shape and unwraps optionals", () => {
    const fields = schemaFields(RAW_IMAGE_SCHEMA);
    const field = (name: string) => {
      const found = fields.find((f) => f.name === name);
      if (!found) throw new Error(`missing field ${name}`);
      return found;
    };
    expect(field("image").kind).toBe("string");
    expect(field("image").required).toBe(true);
    expect(field("internal_port").kind).toBe("number");
    expect(field("domain").kind).toBe("string");
    expect(field("domain").required).toBe(false);
    expect(field("behind_cloudflare").kind).toBe("boolean");
    expect(field("env").kind).toBe("map");
    expect(field("memory_mb").kind).toBe("number");
    expect(field("memory_mb").schema.maximum).toBe(1048576);
  });
});

describe("blueprintDisplayName", () => {
  it("prefers backend display metadata over generated schema titles", () => {
    expect(
      blueprintDisplayName({
        id: "postgres",
        version: 1,
        category: "Databases",
        icon: "postgres",
        display_name: "PostgreSQL",
        description: "Deploy PostgreSQL",
        inputs_schema: { title: "PostgresInputs" },
        actions: [],
      }),
    ).toBe("PostgreSQL");
  });

  it("does not show generated Inputs model names as blueprint names", () => {
    expect(
      blueprintDisplayName({
        id: "uptime-kuma",
        version: 1,
        category: "Monitoring",
        icon: "activity",
        display_name: "",
        description: "Deploy Uptime Kuma",
        inputs_schema: { title: "Uptime-kumaInputs" },
        actions: [],
      }),
    ).toBe("Uptime Kuma");
  });
});

describe("SchemaForm", () => {
  it("renders a field per schema property", () => {
    render(<SchemaForm schema={RAW_IMAGE_SCHEMA} onSubmit={() => {}} />);
    expect(screen.getByLabelText("Image")).toBeInTheDocument();
    expect(screen.getByLabelText("Internal Port")).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("Behind Cloudflare")).toHaveAttribute("type", "checkbox");
    expect(screen.getByRole("button", { name: "Add entry" })).toBeInTheDocument();
  });

  it("blocks submit until required fields are filled", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<SchemaForm schema={RAW_IMAGE_SCHEMA} onSubmit={onSubmit} submitLabel="Create" />);

    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("Image is required")).toBeInTheDocument();
    expect(screen.getByText("Internal Port is required")).toBeInTheDocument();
  });

  it("enforces numeric bounds from the schema", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<SchemaForm schema={RAW_IMAGE_SCHEMA} onSubmit={onSubmit} submitLabel="Create" />);

    await user.type(screen.getByLabelText("Image"), "ghcr.io/acme/app:v1");
    await user.type(screen.getByLabelText("Internal Port"), "99999");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("Internal Port must be ≤ 65535")).toBeInTheDocument();
  });

  it("submits coerced values and omits empty optionals", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<SchemaForm schema={RAW_IMAGE_SCHEMA} onSubmit={onSubmit} submitLabel="Create" />);

    await user.type(screen.getByLabelText("Image"), "ghcr.io/acme/app:v1");
    await user.type(screen.getByLabelText("Internal Port"), "3000");
    await user.click(screen.getByLabelText("Behind Cloudflare"));
    await user.click(screen.getByRole("button", { name: "Add entry" }));
    await user.type(screen.getByLabelText("env name 1"), "NODE_ENV");
    await user.type(screen.getByLabelText("env value 1"), "production");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(onSubmit).toHaveBeenCalledWith({
      image: "ghcr.io/acme/app:v1",
      internal_port: 3000,
      behind_cloudflare: true,
      env: { NODE_ENV: "production" },
    });
    // domain and memory_mb left blank → omitted, so pydantic defaults apply.
    const submitted = onSubmit.mock.calls[0]?.[0];
    expect(submitted).not.toHaveProperty("domain");
    expect(submitted).not.toHaveProperty("memory_mb");
  });

  it("rejects map rows without a key and duplicate keys", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<SchemaForm schema={RAW_IMAGE_SCHEMA} onSubmit={onSubmit} submitLabel="Create" />);

    await user.type(screen.getByLabelText("Image"), "nginx:1.27");
    await user.type(screen.getByLabelText("Internal Port"), "80");
    await user.click(screen.getByRole("button", { name: "Add entry" }));
    await user.type(screen.getByLabelText("env value 1"), "orphan");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("Env: every entry needs a name")).toBeInTheDocument();
  });

  it("renders an enum field as a version dropdown defaulting to the latest", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const schema: JsonSchema = {
      type: "object",
      properties: {
        version: {
          type: "string",
          title: "Version",
          enum: ["17", "16", "15"],
          default: "17",
          description: "PostgreSQL version",
        },
      },
    };
    render(<SchemaForm schema={schema} onSubmit={onSubmit} submitLabel="Create" />);

    const select = screen.getByLabelText("Version");
    expect(select.tagName).toBe("SELECT");
    expect(
      within(select as HTMLElement)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["17", "16", "15"]);
    // Defaults to the latest, but is changeable.
    await user.selectOptions(select, "15");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).toHaveBeenCalledWith({ version: "15" });
  });

  it("classifies an enum string field as a select", () => {
    const fields = schemaFields({
      type: "object",
      properties: { version: { type: "string", enum: ["16", "15"] } },
    });
    expect(fields[0]?.kind).toBe("select");
  });

  it("renders secret-flagged fields as password inputs", () => {
    const schema: JsonSchema = {
      type: "object",
      required: ["api_key"],
      properties: {
        api_key: { type: "string", title: "Api Key", secret: true },
      },
    };
    render(<SchemaForm schema={schema} onSubmit={() => {}} />);
    expect(screen.getByLabelText("Api Key")).toHaveAttribute("type", "password");
  });
});
