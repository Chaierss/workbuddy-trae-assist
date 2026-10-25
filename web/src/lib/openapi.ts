/**
 * OpenAPI 规格的最小类型与只读解析工具。
 *
 * 接口文档页直接读 `/api/openapi.json` 渲染，不再依赖桌面端是否打包了
 * Swagger UI 静态资源 —— 规格是运行期生成的，页面也跟着运行期刷新。
 */

export interface OpenApiSchema {
  $ref?: string;
  type?: string | string[];
  format?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  default?: unknown;
  nullable?: boolean;
  items?: OpenApiSchema;
  properties?: Record<string, OpenApiSchema>;
  required?: string[];
  allOf?: OpenApiSchema[];
  oneOf?: OpenApiSchema[];
  anyOf?: OpenApiSchema[];
  additionalProperties?: boolean | OpenApiSchema;
  [key: string]: unknown;
}

export interface OpenApiMediaType {
  schema?: OpenApiSchema;
}

export interface OpenApiParameter {
  name: string;
  in: string;
  required?: boolean;
  description?: string;
  schema?: OpenApiSchema;
}

export interface OpenApiRequestBody {
  required?: boolean;
  description?: string;
  content?: Record<string, OpenApiMediaType>;
}

export interface OpenApiResponse {
  description?: string;
  content?: Record<string, OpenApiMediaType>;
}

export interface OpenApiOperation {
  tags?: string[];
  summary?: string;
  description?: string;
  operationId?: string;
  deprecated?: boolean;
  parameters?: OpenApiParameter[];
  requestBody?: OpenApiRequestBody;
  responses?: Record<string, OpenApiResponse>;
}

export interface OpenApiSpec {
  openapi?: string;
  info?: { title?: string; version?: string; description?: string };
  servers?: { url: string; description?: string }[];
  tags?: { name: string; description?: string }[];
  paths?: Record<string, Record<string, unknown>>;
  components?: { schemas?: Record<string, OpenApiSchema>; [key: string]: unknown };
}

export interface OperationEntry {
  method: string;
  path: string;
  operation: OpenApiOperation;
}

export interface OperationGroup {
  /** `null` 表示这份接口没打标签，界面上归到「其他」。 */
  tag: string | null;
  description: string;
  operations: OperationEntry[];
}

export interface SchemaRow {
  field: string;
  type: string;
  required: boolean;
  description: string;
  depth: number;
}

const METHODS = ["get", "post", "put", "patch", "delete", "head", "options"];

/** 按标签分组；组内按路径、再按固定方法顺序排，保证每次渲染顺序一致。 */
export function groupOperations(spec: OpenApiSpec | null): OperationGroup[] {
  if (!spec?.paths) return [];
  const tagDescriptions = new Map((spec.tags ?? []).map((tag) => [tag.name, tag.description ?? ""]));
  const groups = new Map<string | null, OperationEntry[]>();

  for (const [path, item] of Object.entries(spec.paths)) {
    const operations = item as Record<string, OpenApiOperation>;
    for (const method of METHODS) {
      const operation = operations[method];
      if (!operation) continue;
      const tag = operation.tags?.[0] ?? null;
      const bucket = groups.get(tag) ?? [];
      bucket.push({ method: method.toUpperCase(), path, operation });
      groups.set(tag, bucket);
    }
  }

  const tagOrder = (spec.tags ?? []).map((tag) => tag.name);
  return [...groups.entries()]
    .map(([tag, operations]) => ({
      tag,
      description: tag ? (tagDescriptions.get(tag) ?? "") : "",
      operations: operations.sort((a, b) => {
        if (a.path !== b.path) return a.path.localeCompare(b.path);
        return METHODS.indexOf(a.method.toLowerCase()) - METHODS.indexOf(b.method.toLowerCase());
      }),
    }))
    .sort((a, b) => {
      const left = a.tag ? tagOrder.indexOf(a.tag) : Number.MAX_SAFE_INTEGER;
      const right = b.tag ? tagOrder.indexOf(b.tag) : Number.MAX_SAFE_INTEGER;
      return left - right;
    });
}

/** `#/components/schemas/Account` → `Account`。 */
export function refName(ref?: string): string {
  if (!ref) return "object";
  return ref.split("/").pop() ?? "object";
}

function resolveRef(spec: OpenApiSpec | null, schema: OpenApiSchema | undefined): OpenApiSchema {
  if (!schema) return {};
  if (!schema.$ref) return schema;
  const name = refName(schema.$ref);
  return spec?.components?.schemas?.[name] ?? { type: name };
}

/** 单行类型文案：`string`、`string[]`、`Account`、`integer(int64)`。 */
export function schemaLabel(spec: OpenApiSpec | null, schema?: OpenApiSchema): string {
  if (!schema) return "any";
  if (schema.$ref) return refName(schema.$ref);
  if (schema.allOf?.length === 1) return schemaLabel(spec, schema.allOf[0]);
  if (schema.enum?.length) return `${schema.type ?? "string"}(${schema.enum.join(" | ")})`;
  if (schema.type === "array" || schema.items) {
    return `${schemaLabel(spec, schema.items)}[]`;
  }
  const type = Array.isArray(schema.type) ? schema.type.join(" | ") : (schema.type ?? "object");
  return schema.format ? `${type}(${schema.format})` : type;
}

/**
 * 把对象结构摊平成「字段 / 类型 / 必填 / 说明」行，供表格渲染。
 *
 * 递归深度上限 3：再深就会在页面上刷出几十行嵌套字段，读起来还不如看标题，
 * 也更接近 Swagger 默认「模型不收全」的取舍。
 */
export function flattenSchema(
  spec: OpenApiSpec | null,
  schema: OpenApiSchema | undefined,
  maxDepth = 3,
): SchemaRow[] {
  const rows: SchemaRow[] = [];
  const seen = new Set<string>();

  function walk(node: OpenApiSchema | undefined, prefix: string, depth: number, required: string[]): void {
    if (!node || depth > maxDepth) return;
    const resolved = resolveRef(spec, node);
    const refKey = node.$ref ? refName(node.$ref) : "";
    if (refKey && seen.has(refKey)) return;
    if (refKey) seen.add(refKey);

    const properties = resolved.properties ?? {};
    for (const [key, child] of Object.entries(properties)) {
      const field = prefix ? `${prefix}.${key}` : key;
      rows.push({
        field,
        type: schemaLabel(spec, child),
        required: (resolved.required ?? required).includes(key),
        description: child.description ?? resolveRef(spec, child).description ?? "",
        depth,
      });
      walk(child, field, depth + 1, []);
    }

    if (!Object.keys(properties).length && resolved.items?.properties) {
      walk(resolved.items, `${prefix}[]`, depth + 1, []);
    }
  }

  walk(schema, "", 0, []);
  return rows;
}

/** 从 content 里挑第一份媒体类型：本项目的接口只有 JSON。 */
export function firstMedia(spec: OpenApiSpec | null, response: OpenApiResponse): OpenApiMediaType | null {
  const content = response.content;
  if (!content) return null;
  return content["application/json"] ?? Object.values(content)[0] ?? null;
}
