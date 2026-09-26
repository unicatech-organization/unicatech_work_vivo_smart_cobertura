"""
Cliente para a API SWE do Siebel.
Executa as chamadas HTTP via fetch() dentro do navegador (via Selenium),
evitando problemas com Cloudflare/SSL e reutilizando a sessão autenticada.
Não interage com a UI — apenas faz chamadas HTTP diretas.
"""
import re
import time
import json


class SiebelAPIClient:
    """Cliente para a API SWE (Siebel Web Engine) via fetch() no browser."""
    
    BASE_URL = "https://vivovendas.vivo.com.br/sales_ext/start.swe"
    
    def __init__(self, driver):
        """
        Args:
            driver: Instância do Selenium WebDriver já conectada ao Siebel.
        """
        self.driver = driver
        
        # Extrai SRN e SWEC da sessão ativa do Siebel
        session_info = driver.execute_script("""
            try {
                var app = SiebelApp.S_App;
                return {
                    srn: app.GetSRN ? app.GetSRN() : '',
                    swec: app.GetSWEC ? app.GetSWEC() : 1
                };
            } catch(e) { return {error: e.message}; }
        """)
        
        if session_info.get("error"):
            raise ValueError(f"Erro ao acessar sessão Siebel: {session_info['error']}")
        
        self.srn = session_info.get("srn", "")
        self.swec = session_info.get("swec", 1)
        
        if not self.srn:
            raise ValueError("Não foi possível extrair o SRN da sessão Siebel")
        
        print(f"[API] Sessão Siebel: SRN={self.srn[:20]}... SWEC={self.swec}")
    
    def _next_swec(self):
        """Incrementa e retorna o próximo SWEC."""
        self.swec += 1
        return self.swec
    
    def _post(self, data: dict) -> str:
        """
        Faz POST para a API SWE via fetch() dentro do navegador.
        Retorna o body da resposta.
        """
        data["SWERPC"] = "1"
        data["SRN"] = self.srn
        data["SWETS"] = str(int(time.time() * 1000))
        
        # Serializa os dados como URL-encoded e executa fetch no browser
        response = self.driver.execute_async_script("""
            var data = arguments[0];
            var url = arguments[1];
            var callback = arguments[2];
            
            // Converte dict para URL-encoded string
            var params = new URLSearchParams();
            for (var key in data) {
                params.append(key, data[key]);
            }
            
            fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': '*/*'
                },
                body: params.toString(),
                credentials: 'include'
            })
            .then(function(resp) { return resp.text(); })
            .then(function(body) { callback({ok: true, body: body}); })
            .catch(function(err) { callback({ok: false, error: err.message}); });
        """, data, self.BASE_URL)
        
        if not response.get("ok"):
            raise RuntimeError(f"Erro na chamada API: {response.get('error')}")
        
        body = response["body"]
        
        # Atualiza SRN e SWEC da resposta
        srn_match = re.search(r'`SRN`([^`]*)`', body)
        if srn_match and srn_match.group(1):
            self.srn = srn_match.group(1)
        swec_match = re.search(r'`SWEC`(\d+)`', body)
        if swec_match:
            self.swec = int(swec_match.group(1))
        
        return body
    
    def search_cnpj(self, cnpj: str) -> dict:
        """
        Pesquisa um CNPJ via API direta (sem interagir com a UI).
        Retorna dict com empresa e produtos raiz.
        """
        data = {
            "s_1_1_13_0": "",
            "s_1_1_12_0": "",
            "s_1_1_11_0": cnpj,
            "s_1_1_8_0": "",
            "s_1_1_1_0": "",
            "s_1_1_2_0": "",
            "s_1_1_9_0": "",
            "SWECmd": "InvokeMethod",
            "SWEVI": "",
            "SWEView": "NV Initial Dealer Account View",
            "SWEApplet": "NV Initial Dealer Search Account Applet",
            "SWEMethod": "NVSearchAccountDealer",
            "SWERowId": "VRId-0",
            "SWEReqRowId": "1",
            "SWERowIds": "",
            "SWEActiveApplet": "NV Initial Dealer Search Account Applet",
            "SWENeedContext": "true",
            "SWEC": str(self._next_swec()),
            "SWEActiveView": "NV Initial Dealer Account View",
        }
        
        body = self._post(data)
        return self._parse_search_response(body)
    
    def search_coverage(self, cep: str, numero: str, cidade: str = "", estado: str = "") -> dict:
        """
        Pesquisa a cobertura de um endereço via CEP e número (sem interagir com a UI).
        Retorna dict com a lista de endereços encontrados e se algum deles é GPON.
        """
        data = {
            "SWEField": "s_4_1_29_0",
            "SWENeedContext": "true",
            "SWER": "0",
            "SWESP": "false",
            "SWEMethod": "NVSearchAddress",
            "SWECmd": "InvokeMethod",
            "SWEDIC": "false",
            "SWEReqRowId": "1",
            "SWEView": "NV Check Address Coverage Only View - Dealer",
            "SWEBID": "-1",
            "SWEApplet": "NV Service Address Query Applet - Dealer",
            "s_4_1_12_0": cep,
            "s_4_1_8_0": numero,
            "s_4_1_16_0": "",
            "s_4_1_9_0": "",
            "s_4_1_2_0": "",
            "s_4_1_10_0": "",
            "s_4_1_3_0": "",
            "s_4_1_11_0": "",
            "s_4_1_4_0": "",
            "s_4_1_17_0": "",
            "s_4_1_1_0": cidade,
            "s_4_1_5_0": estado,
            "SWEVI": "",
            "SWERowId": "VRId-0",
            "SWERowIds": "",
            "SWEActiveApplet": "NV Service Address Query Applet - Dealer",
            "SWEC": str(self._next_swec()),
            "SWEActiveView": "NV Check Address Coverage Only View - Dealer",
        }

        body = self._post(data)
        enderecos = self._parse_coverage_response(body)
        return {
            "enderecos": enderecos,
            "is_gpon": any(e.get("Tecnologia Acesso") == "GPON" for e in enderecos),
        }

    def check_coverage_details(self, row_id: str = "VRId-0") -> dict:
        """
        Executa NVCheckCoverage sobre o endereço selecionado (equivale a
        clicar em "Consultar" na tela de cobertura). Deve ser chamado logo
        após search_coverage, que deixa o endereço selecionado na sessão.
        Retorna os campos definitivos de cobertura (tecnologia de acesso,
        rede, notas completas etc.)
        """
        data = {
            "SWEField": "s_3_1_64_0",
            "SWENeedContext": "true",
            "SWER": "65535",
            "SWESP": "false",
            "SWEMethod": "NVCheckCoverage",
            "SWECmd": "InvokeMethod",
            "SWEDIC": "false",
            "SWEReqRowId": "1",
            "SWEView": "NV Check Address Coverage Only View - Dealer",
            "SWEBID": "-1",
            "SWEApplet": "NV Check Coverage Applet - Dealer",
            "SWEVI": "",
            "SWERowId": row_id,
            "SWERowIds": "",
            "SWEActiveApplet": "NV Check Coverage Applet - Dealer",
            "SWEC": str(self._next_swec()),
            "SWEActiveView": "NV Check Address Coverage Only View - Dealer",
        }

        body = self._post(data)
        fields = self._parse_coverage_details(body)
        return {
            "fields": fields,
            "is_gpon": fields.get("GVT Access Technology Calc") == "GPON",
        }

    def check_gpon(self, cep: str, numero: str, cidade: str = "", estado: str = "") -> bool:
        """
        Fluxo completo: pesquisa o endereço por CEP/número e verifica se a
        tecnologia de acesso definitiva é GPON.
        """
        self.search_coverage(cep, numero, cidade, estado)
        details = self.check_coverage_details()
        return details["is_gpon"]

    def expand_product(self, product_row_id: str, account_row_id: str) -> list:
        """
        Expande um nó da árvore de produtos (sem clicar na UI).
        Retorna lista de subprodutos.
        """
        data = {
            "SWER": "0",
            "SWEReqRowId": "1",
            "s_4_1_12_0": "",
            "SWECmd": "InvokeMethod",
            "SWEVI": "",
            "SWEView": "NV Initial Dealer Account View",
            "SWEApplet": "NV Initial SIS OM Products & Services Root List Applet",
            "SWEMethod": "Expand",
            "SWERowId": product_row_id,
            "SWERowIds": f"SWERowId0={account_row_id}",
            "SWEActiveApplet": "NV Initial SIS OM Products & Services Root List Applet",
            "SWENeedContext": "true",
            "SWEC": str(self._next_swec()),
            "SWEActiveView": "NV Initial Dealer Account View",
        }
        
        body = self._post(data)
        return self._parse_expand_response(body)
    
    def expand_all_recursive(self, account_id: str, products: list) -> list:
        """
        Expande recursivamente todos os produtos que têm filhos.
        Retorna a lista completa de produtos únicos (todos os níveis).
        """
        all_products_dict = {}
        for p in products:
            p_id = p.get("Id")
            if p_id:
                # Se já existe e o novo tem menos informações, mantém o existente
                if p_id in all_products_dict and not p.get("GVT Status"):
                    continue
                all_products_dict[p_id] = p
                
        to_expand = [p.get("Id") for p in products if p.get("Has Children") == "Y" and p.get("Id")]
        expanded_ids = set()
        
        while to_expand:
            prod_id = to_expand.pop(0)
            if prod_id in expanded_ids:
                continue
                
            name = all_products_dict.get(prod_id, {}).get("GVT Product Name Calc", prod_id)
            print(f"  [API] Expandindo: {name} (ID={prod_id})...")
            
            try:
                expanded_ids.add(prod_id)
                children = self.expand_product(prod_id, account_id)
                
                for child in children:
                    c_id = child.get("Id")
                    if not c_id:
                        continue
                    
                    if c_id not in all_products_dict or child.get("GVT Status"):
                        all_products_dict[c_id] = child
                        
                    if child.get("Has Children") == "Y" and c_id not in expanded_ids and c_id not in to_expand:
                        to_expand.append(c_id)
                
                time.sleep(0.3)
            except Exception as e:
                print(f"  [AVISO] Erro ao expandir {name}: {e}")
        
        all_products = list(all_products_dict.values())
        
        for p in all_products:
            parent_id = p.get("GVT Parent Hierarchy Item Id")
            p["Parent Id"] = parent_id if parent_id else -1
            
            level = 0
            current_parent_id = parent_id
            visited = set()
            while current_parent_id and current_parent_id not in visited:
                visited.add(current_parent_id)
                parent_prod = all_products_dict.get(current_parent_id)
                if parent_prod:
                    level += 1
                    current_parent_id = parent_prod.get("GVT Parent Hierarchy Item Id")
                else:
                    level += 1
                    break
            p["Hierarchy Level"] = level
            
        return all_products
    
    def _parse_search_response(self, body: str) -> dict:
        """
        Parseia a resposta SWE da pesquisa.
        O formato usa backticks como delimitador e N*valor para campos.
        """
        result = {"empresa": None, "produtos": []}
        
        # Verifica se teve erro/alerta
        if "Status`Completed" not in body and "Status`OK" not in body:
            # Toleramos o caso em que a busca simplesmente não retornou nada
            if "Cliente não encontrado" in body or "nenhum registro" in body.lower():
                return result
                
            result["error"] = "Resposta sem status de sucesso"
            return result
        
        # Extrai dados da empresa (S_BC2 = Account)
        empresa_match = re.search(
            r'OP`iw`bc`S_BC2`1`0`FieldValues`0`ValueArray`(.*?)`',
            body
        )
        if empresa_match:
            values = self._parse_value_array(empresa_match.group(1))
            if len(values) >= 3:
                result["empresa"] = {
                    "name": values[0],
                    "address": values[1],
                    "id": values[2],
                }
        
        # Extrai produtos (S_BC4 = Products)
        self._extract_products(body, result)
        
        return result
    
    def _parse_expand_response(self, body: str) -> list:
        """Parseia a resposta de expand e retorna os subprodutos."""
        result = {"produtos": []}
        self._extract_products(body, result)
        return result["produtos"]

    def _parse_coverage_response(self, body: str) -> list:
        """Extrai os endereços/cobertura do body SWE (S_BC1)."""
        coverage_fields = [
            "Unknown0",
            "Endereco",
            "Disponibilidade",
            "Mensagem",
            "Intervalo",
            "Tecnologia Acesso",
            "Rede",
            "Bairro",
            "CNL",
            "Codigo CNL",
            "Distrito",
            "AT",
            "ES",
            "Id",
        ]

        result = []
        matches = re.finditer(
            r'OP`iw`bc`S_BC1`1`0`FieldValues`0`ValueArray`(.*?)`',
            body
        )
        for match in matches:
            values = self._parse_value_array(match.group(1))
            if len(values) > 1 and values[1]:  # Ignora linhas vazias
                endereco = {}
                for i, field in enumerate(coverage_fields):
                    if i < len(values):
                        endereco[field] = values[i]
                result.append(endereco)

        return result

    @staticmethod
    def _parse_coverage_details(body: str) -> dict:
        """
        Extrai os campos nomeados (ndw) retornados pelo NVCheckCoverage.
        Formato: OP`ndw`bc`S_BC1`f`<Campo>`2`0`FieldValues`0`ValueArray`N*<valor>`FieldArray`...
        O valor é lido por comprimento (N) em vez de até o próximo backtick,
        pois campos como "GVT Coverage Notes" contêm quebras de linha.
        """
        fields = {}
        for match in re.finditer(
            r'OP`ndw`bc`S_BC1`f`([^`]+)`2`0`FieldValues`0`ValueArray`(\d+)\*',
            body
        ):
            field_name = match.group(1)
            length = int(match.group(2))
            value_start = match.end()
            fields[field_name] = body[value_start:value_start + length]

        return fields

    def _extract_products(self, body: str, result: dict):
        """Extrai produtos do body SWE e adiciona ao result."""
        product_matches = re.finditer(
            r'OP`iw`bc`S_BC4`1`0`FieldValues`0`ValueArray`(.*?)`',
            body
        )
        
        # Campos dos produtos na ordem que aparecem no ValueArray
        product_fields = [
            "GVT Product Name Calc",
            "Serial Number",
            "GVT Service Address",
            "GVT Covered Product",
            "GVT Status",
            "GVT Original Order Integration Id",
            "Prod Prom Name",
            "GVT Invalid Offer Flg Calc",
            "GVT Display Voice Technology",
            "GVT Display Access Technology",
            "GVT Bill Prof Seq",
            "Created",
            "GVT Install Date",
            "GVT Commitment Calc",
            "GVT Phone Number Ported Flg",
            "Integration Id",
            "GVT Exhibition Mode",
            "NV Fake Number",
            "NV Duplo Acesso",
            "Quantity",
            "NV Mig MOTF",
            "Id",
            "Outline Number",
            "Last Child Info",
            "Has Children",
            "Is Expanded",
            "GVT Parent Hierarchy Item Id"
        ]
        
        for match in product_matches:
            values = self._parse_value_array(match.group(1))
            if values and values[0]:  # Ignora registros vazios
                product = {}
                for i, field in enumerate(product_fields):
                    if i < len(values):
                        product[field] = values[i]
                result["produtos"].append(product)
    
    @staticmethod
    def _parse_value_array(raw: str) -> list:
        """
        Parseia o formato N*valor do Siebel.
        Ex: '26*CONDOMINIO PARQUE TAQUARAL52*PE. DOMINGOS...'
        O número antes de * indica o comprimento do valor.
        """
        values = []
        pos = 0
        while pos < len(raw):
            # Encontra o próximo N*
            star_pos = raw.find('*', pos)
            if star_pos == -1:
                break
            
            try:
                length = int(raw[pos:star_pos])
            except ValueError:
                break
            
            value_start = star_pos + 1
            value_end = value_start + length
            value = raw[value_start:value_end]
            values.append(value)
            pos = value_end
        
        return values
