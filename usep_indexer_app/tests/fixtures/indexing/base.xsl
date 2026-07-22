<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:tei="http://www.tei-c.org/ns/1.0">
  <xsl:output method="xml" encoding="UTF-8"/>
  <xsl:template match="/">
    <add commitWithin="999">
      <doc>
        <field name="researcher_extension">first</field>
        <field name="id"><xsl:value-of select="/tei:TEI/tei:teiHeader/tei:fileDesc/tei:publicationStmt/tei:idno/@xml:id"/></field>
        <xsl:for-each select="/tei:TEI/tei:teiHeader/tei:fileDesc/tei:sourceDesc/tei:listBibl/tei:bibl/tei:ptr">
          <field name="bib_ids"><xsl:value-of select="@target"/></field>
        </xsl:for-each>
        <xsl:choose>
          <xsl:when test="normalize-space(/tei:TEI/tei:text/tei:body/tei:div[@type='edition']/tei:ab)">
            <field name="status">transcription</field>
          </xsl:when>
          <xsl:when test="normalize-space(/tei:TEI/tei:teiHeader/tei:fileDesc/tei:sourceDesc/tei:msDesc/tei:physDesc)">
            <field name="status">metadata</field>
          </xsl:when>
          <xsl:otherwise><field name="status">bib_only</field></xsl:otherwise>
        </xsl:choose>
        <field name="researcher_extension">second</field>
      </doc>
    </add>
  </xsl:template>
</xsl:stylesheet>
