-- What pandoc cannot write in a .pptx on its own: the footer line and the size of the code.

-- A `::: footer` block becomes one line at the bottom of its slide, below the content's card: a text box of its own.
local FOOTER = [[
<p:sp><p:nvSpPr><p:cNvPr id="91" name="Footer line"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="930750" y="9480000"/><a:ext cx="16318500" cy="560000"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>
<p:txBody><a:bodyPr wrap="square" anchor="ctr"/><a:lstStyle/><a:p><a:r><a:rPr lang="it-IT" sz="2400">
<a:solidFill><a:srgbClr val="0F1A38"/></a:solidFill><a:latin typeface="Bricolage Grotesque"/></a:rPr>
<a:t>%s</a:t></a:r></a:p></p:txBody></p:sp>]]

-- A line of code: 64 characters in the content's box. A break needs the size too: it would be as tall as the text.
local FONT = '<a:rPr lang="en-US" sz="1800" noProof="1"><a:latin typeface="Courier New"/><a:cs typeface="Courier New"/></a:rPr>'
local CODE = "<a:r>" .. FONT .. "<a:t>%s</a:t></a:r>"
local BREAK = "<a:br>" .. FONT .. "</a:br>"

local ENTITIES = { ["&"] = "&amp;", ["<"] = "&lt;", [">"] = "&gt;" }

local function fill(template, text)
  local escaped = text:gsub("[&<>]", ENTITIES)
  return (template:gsub("%%s", function() return escaped end))
end

function Div(div)
  if FORMAT == "pptx" and div.classes:includes("footer") then
    return pandoc.RawBlock("openxml", fill(FOOTER, pandoc.utils.stringify(div)))
  end
end

-- pandoc writes code at the size of the text. The indentation is kept by no-break spaces.
function CodeBlock(block)
  if FORMAT ~= "pptx" then
    return nil
  end
  local inlines = pandoc.Inlines({})
  for line in (block.text .. "\n"):gmatch("(.-)\n") do
    if #inlines > 0 then
      inlines:insert(pandoc.RawInline("openxml", BREAK))
    end
    local kept = line:gsub("^ +", function(spaces) return ("\u{A0}"):rep(#spaces) end)
    inlines:insert(pandoc.RawInline("openxml", fill(CODE, kept)))
  end
  return pandoc.Para(inlines)
end
