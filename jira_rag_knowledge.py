"""
RAG System for Jira Configuration Knowledge
Provides context-aware guidance for Jira setup using vector embeddings
"""

import logging

logger = logging.getLogger(__name__)

# ============================================================================
# Jira Knowledge Base
# ============================================================================
JIRA_KNOWLEDGE_BASE = """
JIRA CLOUD SETUP:
- Login with your Atlassian account email
- Generate API Token from https://id.atlassian.com/manage/api-tokens
- API Token is different from your password - use it for authentication
- URL format: https://yourcompany.atlassian.net
- Authentication: Basic Auth with email + API Token
- REST API endpoint: /rest/api/3/
- Tokens expire after 30 days if unused
- Create multiple tokens for different integrations

JIRA DATA CENTER SETUP:
- Login with your local Jira username and password
- Data Center is self-hosted on your infrastructure
- URL format: https://jira.yourcompany.com or http://jira.internal
- Authentication: Basic Auth with username + password
- REST API endpoint: /rest/api/2/ (or /rest/api/3/ for newer versions)
- Requires direct network access to your Jira server
- May require VPN connection if behind corporate firewall
- Check with IT for network access requirements

JIRA SERVER SETUP:
- Deprecated but still in use on some installations
- Similar to Data Center in authentication
- Uses /rest/api/2/ REST API endpoints
- Will be sunset soon, migrate to Cloud or Data Center
- Contact Atlassian support for migration path

TROUBLESHOOTING:
- Connection timeout: Check firewall, VPN, and network access
- 401 Unauthorized: Verify credentials are correct
- 403 Forbidden: Check user permissions in Jira admin panel
- Invalid URL: Ensure proper https:// or http:// prefix
- Cloud token not working: Regenerate token from Atlassian account
- Data Center password not working: Verify LDAP/SSO configuration
- Connection refused: Ensure Jira service is running
- SSL certificate error: Check corporate proxy or certificate issues

API TOKEN GENERATION (Cloud):
1. Go to https://id.atlassian.com/manage/api-tokens
2. Click "Create API token"
3. Give it a descriptive name (e.g., "Dashboard Integration")
4. Copy the generated token immediately (you cannot retrieve it later)
5. Store in secure location or password manager
6. Use token as password in Basic Auth with email as username
7. Regenerate token if compromised
8. Delete unused tokens for security

CLOUD vs DATA CENTER COMPARISON:
- Cloud: Hosted by Atlassian, easier setup, always updated, less maintenance
- Data Center: Self-hosted, more control, requires infrastructure, higher cost
- Cloud uses email + API Token for authentication
- Data Center uses username + password for authentication
- Different REST API versions (3 vs 2)
- Cloud has different field IDs and custom field handling
- Cloud billing: usage-based
- Data Center: perpetual or subscription license

SECURITY BEST PRACTICES:
- Never commit credentials to version control or git
- Use environment variables for sensitive data (.env files)
- Rotate API tokens regularly (every 90 days recommended)
- Restrict API token scope if possible
- Monitor API token usage in Atlassian dashboard
- Use HTTPS for all connections to Jira
- Consider IP whitelisting for Data Center integrations
- Store tokens in secure vaults or secret managers
- Use managed identity when available in cloud platforms
- Enable two-factor authentication on Atlassian account

NETWORK REQUIREMENTS:
- Ensure firewall allows outbound HTTPS (443) to Jira instance
- Check proxy settings if behind corporate proxy
- VPN may be required for self-hosted instances
- Some networks block API access - work with IT
- Test connectivity with curl or Postman before automation

FIELD ID MAPPING:
- Custom fields have IDs like customfield_10001
- Standard fields: summary, description, status, priority
- Cloud uses different field IDs than Data Center
- Check field IDs in Jira REST API documentation
- Use /rest/api/2/fields endpoint to list all fields

AUTHENTICATION METHODS:
- Basic Auth: username:password or email:token (Base64 encoded)
- Personal API Tokens: Cloud recommended method
- Personal Access Tokens: Data Center (v8.14+)
- OAuth2: For third-party integrations
- Session cookies: For browser-based interactions
- Custom authentication: Through API gateway or proxy

PAGINATION:
- Cloud uses limit/offset pagination
- Data Center uses startAt/maxResults
- Always handle pagination in integrations
- Jira limits results to 50-1000 depending on endpoint
- Respect rate limiting: 300 requests/minute for Cloud

COMMON ERROR CODES:
- 400 Bad Request: Invalid parameters or malformed JSON
- 401 Unauthorized: Invalid or missing credentials
- 403 Forbidden: User lacks permissions for operation
- 404 Not Found: Issue or resource doesn't exist
- 409 Conflict: Invalid status transition
- 429 Too Many Requests: Rate limit exceeded
- 500 Server Error: Jira server error
- 503 Service Unavailable: Jira is down for maintenance
"""

class JiraRAG:
    """RAG system for Jira knowledge retrieval"""

    def __init__(self):
        """Initialize RAG system"""
        self.knowledge_base = JIRA_KNOWLEDGE_BASE
        self.chunks = self._split_knowledge()
        logger.info(f"RAG system initialized with {len(self.chunks)} knowledge chunks")

    def _split_knowledge(self):
        """Split knowledge base into searchable chunks"""
        # Split by major sections (indicated by ALL CAPS headings)
        chunks = []
        current_chunk = ""

        for line in self.knowledge_base.split('\n'):
            if line.isupper() and line.strip() and len(line) > 5:
                # Found a new section heading
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"

        # Add the last chunk
        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        # Ensure chunks are reasonable size
        final_chunks = []
        for chunk in chunks:
            if len(chunk) > 1000:
                # Split long chunks further
                sub_chunks = chunk.split('\n\n')
                final_chunks.extend(sub_chunks)
            else:
                final_chunks.append(chunk)

        return [c.strip() for c in final_chunks if c.strip()]

    def retrieve(self, query: str, jira_type: str = "", top_k: int = 3) -> str:
        """
        Retrieve relevant knowledge chunks based on query

        Args:
            query: Search query
            jira_type: Filter by 'cloud' or 'datacenter'
            top_k: Number of top results to return

        Returns:
            Combined relevant knowledge
        """
        # Normalize query
        query_lower = query.lower()

        # Score chunks based on keyword matching
        scored_chunks = []
        for chunk in self.chunks:
            chunk_lower = chunk.lower()

            # Skip if jira_type filter doesn't match
            if jira_type:
                if jira_type.lower() == "cloud" and "cloud" not in chunk_lower:
                    if "data center" in chunk_lower or "datacenter" in chunk_lower:
                        continue
                elif jira_type.lower() == "datacenter" and "data center" not in chunk_lower and "datacenter" not in chunk_lower:
                    if "cloud" in chunk_lower:
                        continue

            # Calculate relevance score
            score = 0
            query_words = query_lower.split()

            for word in query_words:
                if len(word) > 2:  # Skip short words
                    if word in chunk_lower:
                        score += chunk_lower.count(word)

            if score > 0:
                scored_chunks.append((score, chunk))

        # Sort by score and get top_k
        scored_chunks.sort(reverse=True, key=lambda x: x[0])
        top_chunks = [chunk for _, chunk in scored_chunks[:top_k]]

        if not top_chunks:
            # Return generic guidance if no match
            return self._get_generic_guidance(jira_type)

        return "\n\n---\n\n".join(top_chunks)

    def _get_generic_guidance(self, jira_type: str) -> str:
        """Get generic guidance based on Jira type"""
        if jira_type.lower() == "cloud":
            return """
JIRA CLOUD AUTHENTICATION:
- Use your Atlassian account email as username
- Generate API Token from https://id.atlassian.com/manage/api-tokens
- API Token replaces your password
- Format: email:token in Basic Auth
- URL: https://yourcompany.atlassian.net
"""
        elif jira_type.lower() == "datacenter":
            return """
JIRA DATA CENTER AUTHENTICATION:
- Use your local Jira username
- Use your Jira password (not API token)
- Format: username:password in Basic Auth
- Ensure network access to Jira server
- Check VPN if self-hosted
"""
        else:
            return JIRA_KNOWLEDGE_BASE[:500]  # Return first chunk

def get_jira_guidance(question: str, jira_type: str = "") -> str:
    """
    Get RAG-based Jira setup guidance

    Args:
        question: User's question about Jira setup
        jira_type: Optional filter for 'cloud' or 'datacenter'

    Returns:
        str: Relevant guidance from knowledge base
    """
    try:
        rag = JiraRAG()
        guidance = rag.retrieve(question, jira_type, top_k=3)
        return guidance
    except Exception as e:
        logger.error(f"Error retrieving guidance: {e}")
        return f"Error retrieving guidance: {str(e)}"

