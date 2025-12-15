#!/bin/bash
# Script for generating SSH keys in the panel container

echo "🔑 Generating SSH keys for Server Panel..."
echo ""

# Create directory for keys
docker exec serverpanel-app mkdir -p /root/.ssh
docker exec serverpanel-app chmod 700 /root/.ssh

# Generate key
docker exec serverpanel-app ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -N ""

echo ""
echo "✅ SSH key successfully created!"
echo ""
echo "📋 Your public key (copy it):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
docker exec serverpanel-app cat /root/.ssh/id_rsa.pub
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📝 Add this key to your servers:"
echo "   ssh user@your-server"
echo "   echo 'YOUR_PUBLIC_KEY' >> ~/.ssh/authorized_keys"
echo "   chmod 600 ~/.ssh/authorized_keys"
echo ""
echo "🎯 Then specify in server settings:"
echo "   - SSH user: root (or your user)"
echo "   - SSH port: 22"
echo "   - Key path: /root/.ssh/id_rsa"
