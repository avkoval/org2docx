-- org2docx pandoc filter (generic, driven by the profile settings).
--
-- Settings arrive as JSON in the ORG2DOCX_SETTINGS environment variable (written by org2docx.py):
--   number_headings    false | true | N     hierarchical heading numbers "1", "1.2", ... (N: levels 1..N only)
--   appendix_lettering bool                 level-1 headings "<appendix_word> <Letter> ..." keep their letter and
--   appendix_word      string               number their children "<Letter>.1", "<Letter>.1.1"
--   appendix_page_break bool                page break before every appendix heading
--   caption_table / caption_figure / caption_separator
--                                           captions become "<word> N<sep> <caption text>"
--   number_uncaptioned_diagrams bool        PlantUML figures without #+CAPTION still get "<figure word> N<sep>"
--   plantuml           bool                 render PlantUML code blocks to PNG (false: keep the source block)
--   diagram_dir        path                 where rendered PNGs go
--   toc, toc_title, toc_levels              insert a Word TOC field at the start of the body, unless the
--                                           document already contains one (raw openxml with a TOC field)
--
-- Headings with the "unnumbered" class (org :UNNUMBERED: t) are not numbered.

local S = pandoc.json.decode(os.getenv("ORG2DOCX_SETTINGS") or "{}")
local outdir = S.diagram_dir or "org2docx-diagrams"
local sep = S.caption_separator or "."

local counters = { 0, 0, 0, 0, 0, 0, 0, 0, 0 }
local appendix = nil
local fig_n, tab_n = 0, 0

local function max_numbered_level()
  local n = S.number_headings
  if n == true then return 9 end
  if type(n) == "number" then return n end
  return 0
end

local function escape_xml(s)
  return (s:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"))
end

local function page_break()
  return pandoc.RawBlock("openxml", '<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
end

local function number_header(h)
  local text = pandoc.utils.stringify(h.content)
  local maxlvl = max_numbered_level()
  if S.appendix_lettering and h.level == 1 then
    local word = S.appendix_word or "Appendix"
    local letter = text:match("^" .. word:gsub("%p", "%%%0") .. "%s+([^%s%.]+)")
    if letter then
      appendix = letter
      counters = { 0, 0, 0, 0, 0, 0, 0, 0, 0 }
      if S.appendix_page_break then return { page_break(), h } end
      return h
    end
    appendix = nil
  end
  if maxlvl == 0 or h.level > maxlvl or h.classes:includes("unnumbered") then return h end
  counters[h.level] = counters[h.level] + 1
  for i = h.level + 1, #counters do counters[i] = 0 end
  local parts = {}
  local first = appendix and 2 or 1
  if appendix then parts[1] = appendix end
  for i = first, h.level do parts[#parts + 1] = tostring(counters[i]) end
  local num = table.concat(parts, ".")
  h.content:insert(1, pandoc.Space())
  h.content:insert(1, pandoc.Str(num))
  return h
end

local function file_exists(p)
  local f = io.open(p, "r")
  if f then f:close() return true end
  return false
end

local function render_plantuml(code)
  -- a named "@startuml name" would make plantuml write name.png instead of the hashed file
  local src = code:gsub("@startuml[^\n]*\n", "@startuml\n", 1)
  if not src:match("@startuml") then src = "@startuml\n" .. src .. "\n@enduml\n" end
  if not src:match("skinparam%s+dpi") then
    src = src:gsub("@startuml\n", "@startuml\nskinparam dpi 200\n", 1)
  end
  local hash = pandoc.sha1(src)
  local png = outdir .. "/" .. hash .. ".png"
  if file_exists(png) then return png end
  local puml = outdir .. "/" .. hash .. ".puml"
  local w = assert(io.open(puml, "w")); w:write(src); w:close()
  local ok = os.execute("plantuml -charset UTF-8 -tpng -o . '" .. puml .. "' >/dev/null 2>&1")
  if not ok or not file_exists(png) then
    io.stderr:write("org2docx: WARNING: plantuml failed for " .. puml .. "; the source block is kept\n")
    return nil
  end
  return png
end

local function prefixed(word, n, inlines)
  local cap = pandoc.List({ pandoc.Str(word .. " " .. n .. sep) })
  if inlines and #inlines > 0 then
    cap:insert(pandoc.Space())
    cap:extend(inlines)
  end
  return cap
end

-- PlantUML block -> Figure (numbered later, in document order, together with image figures).
-- Returns nil when rendering is off or fails, so the source block stays.
local function diagram(code, caption_inlines, id)
  if not S.plantuml then return nil end
  local png = render_plantuml(code)
  if not png then return nil end
  local img = pandoc.Image({}, png, "")
  local cap = {}
  if caption_inlines and #caption_inlines > 0 then cap = { pandoc.Plain(caption_inlines) } end
  return pandoc.Figure(pandoc.Plain({ img }), cap, pandoc.Attr(id or "", {}, { ["org2docx-diagram"] = "1" }))
end

local function is_plantuml(b)
  return b.t == "CodeBlock" and (b.classes:includes("plantuml") or b.classes:includes("puml"))
end

-- org "#+CAPTION:" on a source block: pandoc wraps it in Div.captioned-content with a Div.caption
local function Div(d)
  if d.classes:includes("captioned-content") then
    local cap, code = nil, nil
    for _, b in ipairs(d.content) do
      if b.t == "Div" and b.classes:includes("caption") then cap = pandoc.utils.blocks_to_inlines(b.content)
      elseif is_plantuml(b) then code = b end
    end
    if code then
      local fig = diagram(code.text, cap, code.identifier)
      if fig then return fig end
    end
  end
end

local function CodeBlock(b)
  if is_plantuml(b) then return diagram(b.text, nil, b.identifier) end
end

-- one sequence for every figure (images with #+CAPTION and PlantUML diagrams), in document order
local function Figure(f)
  local inl = pandoc.utils.blocks_to_inlines(f.caption.long or {})
  local is_diagram = f.attr.attributes["org2docx-diagram"] ~= nil
  if is_diagram then f.attr.attributes["org2docx-diagram"] = nil end
  if #inl == 0 and not (is_diagram and S.number_uncaptioned_diagrams) then return f end
  fig_n = fig_n + 1
  f.caption.long = { pandoc.Plain(prefixed(S.caption_figure or "Figure", fig_n, inl)) }
  return f
end

local function Table(t)
  local inl = pandoc.utils.blocks_to_inlines(t.caption.long or {})
  if #inl > 0 then
    tab_n = tab_n + 1
    t.caption.long = { pandoc.Plain(prefixed(S.caption_table or "Table", tab_n, inl)) }
  end
  return t
end

local function has_toc_field(blocks)
  local found = false
  blocks:walk({
    RawBlock = function(r)
      if r.format == "openxml" and r.text:match("instrText[^>]*>%s*TOC%s") then found = true end
    end,
  })
  return found
end

local function toc_blocks(has_title)
  local levels = tonumber(S.toc_levels) or 3
  local out = pandoc.List()
  if has_title then out:insert(page_break()) end
  out:insert(pandoc.RawBlock("openxml",
    '<w:p><w:pPr><w:pStyle w:val="TOCHeading"/></w:pPr><w:r><w:t>' .. escape_xml(S.toc_title or "Contents")
    .. '</w:t></w:r></w:p>'))
  out:insert(pandoc.RawBlock("openxml",
    '<w:p><w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o "1-'
    .. levels .. '" \\h \\z \\u </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    .. '<w:r><w:t>' .. escape_xml(S.toc_placeholder or "...") .. '</w:t></w:r>'
    .. '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'))
  out:insert(page_break())
  return out
end

function Pandoc(doc)
  -- order: diagrams and captions first (document order), then the heading numbers
  doc = doc:walk({ Div = Div })
  doc = doc:walk({ CodeBlock = CodeBlock })
  doc = doc:walk({ Figure = Figure })
  doc = doc:walk({ Table = Table })
  doc = doc:walk({ Header = number_header })
  if S.toc and not has_toc_field(doc.blocks) then
    local has_title = doc.meta.title ~= nil
    local blocks = toc_blocks(has_title)
    blocks:extend(doc.blocks)
    doc.blocks = blocks
  end
  return doc
end

return { { Pandoc = Pandoc } }
