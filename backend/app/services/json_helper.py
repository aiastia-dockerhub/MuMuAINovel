"""JSON 处理工具类"""
import json
import re
from typing import Any, Dict, List, Union
from app.logger import get_logger

try:
    import json5
    HAS_JSON5 = True
except ImportError:
    HAS_JSON5 = False

logger = get_logger(__name__)


# 中文引号/括号到ASCII的映射
_QUOTE_MAP = {
    '\u201c': '"',  # " → "
    '\u201d': '"',  # " → "
    '\u2018': "'",  # ' → '
    '\u2019': "'",  # ' → '
    '\u300e': '"',  # 『 → "
    '\u300f': '"',  # 』 → "
    '\u300c': '"',  # 「 → "
    '\u300d': '"',  # 」 → "
}


def _fix_json_string_values(text: str) -> str:
    """
    修复JSON字符串值中的常见问题：
    1. 裸换行符/制表符 → 转义
    2. 字符串值内的中文引号 → 转义为ASCII引号（避免破坏JSON结构）
    3. 结构位置的中文引号 → 直接替换为ASCII引号
    
    AI生成的JSON常在字符串值中插入未转义的换行符和中文引号。
    此函数遍历文本，区分字符串内外，分别处理。
    """
    if not text or '"' not in text:
        return text
    
    result = []
    i = 0
    in_string = False
    fixed_count = 0
    
    while i < len(text):
        c = text[i]
        
        if c == '"' and not in_string:
            # 进入字符串
            in_string = True
            result.append(c)
            i += 1
            continue
        
        if in_string:
            if c == '\\':
                # 转义字符，检查下一个字符是否合法
                if i + 1 < len(text):
                    next_c = text[i + 1]
                    # JSON 合法转义：\" \\ \/ \b \f \n \r \t \uXXXX
                    if next_c in ('"', '\\', '/', 'b', 'f', 'n', 'r', 't'):
                        # 合法转义，直接保留
                        result.append(c)
                        result.append(next_c)
                        i += 2
                        continue
                    elif next_c == 'u':
                        # Unicode 转义 \uXXXX，检查是否有4个十六进制字符
                        if i + 5 < len(text) and all(text[i+2+k] in '0123456789abcdefABCDEF' for k in range(4)):
                            result.append(text[i:i+6])
                            i += 6
                            continue
                        else:
                            # 不完整的unicode转义，去掉反斜杠
                            result.append(next_c)
                            fixed_count += 1
                            i += 2
                            continue
                    else:
                        # 非法转义字符（如 \c \p \d 等），去掉反斜杠只保留字符
                        result.append(next_c)
                        fixed_count += 1
                        i += 2
                        continue
                else:
                    # 末尾孤立的反斜杠，去掉
                    fixed_count += 1
                    i += 1
                    continue
            
            if c == '"':
                # 字符串结束
                in_string = False
                result.append(c)
                i += 1
                continue
            
            if c == '\n':
                # 裸换行符 → 替换为转义换行
                result.append('\\')
                result.append('n')
                fixed_count += 1
                i += 1
                continue
            
            if c == '\r':
                # 裸回车符 → 忽略或替换
                if i + 1 < len(text) and text[i + 1] == '\n':
                    result.append('\\')
                    result.append('n')
                    fixed_count += 1
                    i += 2
                else:
                    result.append('\\')
                    result.append('n')
                    fixed_count += 1
                    i += 1
                continue
            
            if c == '\t':
                # 裸制表符 → 替换为转义制表符
                result.append('\\')
                result.append('t')
                fixed_count += 1
                i += 1
                continue
            
            # 字符串值内的中文引号 → 转义为 \"（避免破坏JSON结构）
            if c in _QUOTE_MAP:
                result.append('\\')
                result.append(_QUOTE_MAP[c])
                fixed_count += 1
                i += 1
                continue
        
        # 非字符串内的字符
        # 结构位置的中文引号 → 直接替换
        if not in_string and c in _QUOTE_MAP:
            result.append(_QUOTE_MAP[c])
            fixed_count += 1
            i += 1
            continue
        
        result.append(c)
        i += 1
    
    if fixed_count > 0:
        logger.debug(f"✅ 修复了{fixed_count}个JSON问题（裸控制字符/中文引号）")
    
    return ''.join(result)


def clean_json_response(text: str) -> str:
    """清洗 AI 返回的 JSON（改进版 - 流式安全）"""
    try:
        if not text:
            logger.warning("⚠️ clean_json_response: 输入为空")
            return text
        
        original_length = len(text)
        logger.debug(f"🔍 开始清洗JSON，原始长度: {original_length}")
        
        # 替换中文逗号/冒号（AI可能在JSON结构位置使用，全局替换是安全的）
        text = text.replace('\uff0c', ',')   # ，→ ,
        text = text.replace('\uff1a', ':')   # ：→ :
        
        # 修复JSON中的中文引号和裸控制字符（上下文感知，区分字符串内外）
        text = _fix_json_string_values(text)
        
        # 去除 markdown 代码块
        text = re.sub(r'^```json\s*\n?', '', text, flags=re.MULTILINE | re.IGNORECASE)
        text = re.sub(r'^```\s*\n?', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
        text = text.strip()
        
        if len(text) != original_length:
            logger.debug(f"   移除markdown后长度: {len(text)}")
        
        # 尝试直接解析（快速路径）
        try:
            json.loads(text)
            logger.debug(f"✅ 直接解析成功，无需清洗")
            return text
        except Exception:
            pass
        
        # 找到第一个 { 或 [
        start = -1
        for i, c in enumerate(text):
            if c in ('{', '['):
                start = i
                break
        
        if start == -1:
            logger.warning(f"⚠️ 未找到JSON起始符号 {{ 或 [")
            logger.debug(f"   文本预览: {text[:200]}")
            return text
        
        if start > 0:
            logger.debug(f"   跳过前{start}个字符")
            text = text[start:]
        
        # 改进的括号匹配算法（更严格的字符串处理）
        stack = []
        i = 0
        end = -1
        in_string = False
        
        while i < len(text):
            c = text[i]
            
            # 处理字符串状态
            if c == '"':
                if not in_string:
                    # 进入字符串
                    in_string = True
                else:
                    # 检查是否是转义的引号
                    num_backslashes = 0
                    j = i - 1
                    while j >= 0 and text[j] == '\\':
                        num_backslashes += 1
                        j -= 1
                    
                    # 偶数个反斜杠表示引号未被转义，字符串结束
                    if num_backslashes % 2 == 0:
                        in_string = False
                
                i += 1
                continue
            
            # 在字符串内部，跳过所有字符
            if in_string:
                i += 1
                continue
            
            # 处理括号（只有在字符串外部才有效）
            if c == '{' or c == '[':
                stack.append(c)
            elif c == '}':
                if len(stack) > 0 and stack[-1] == '{':
                    stack.pop()
                    if len(stack) == 0:
                        end = i + 1
                        logger.debug(f"✅ 找到JSON结束位置: {end}")
                        break
                elif len(stack) > 0:
                    # 括号不匹配，可能是损坏的JSON，尝试继续
                    logger.warning(f"⚠️ 括号不匹配：遇到 }} 但栈顶是 {stack[-1]}")
                else:
                    # 栈为空遇到 }，忽略多余的闭合括号
                    logger.warning(f"⚠️ 遇到多余的 }}，忽略")
            elif c == ']':
                if len(stack) > 0 and stack[-1] == '[':
                    stack.pop()
                    if len(stack) == 0:
                        end = i + 1
                        logger.debug(f"✅ 找到JSON结束位置: {end}")
                        break
                elif len(stack) > 0:
                    # 括号不匹配，可能是损坏的JSON，尝试继续
                    logger.warning(f"⚠️ 括号不匹配：遇到 ] 但栈顶是 {stack[-1]}")
                else:
                    # 栈为空遇到 ]，忽略多余的闭合括号
                    logger.warning(f"⚠️ 遇到多余的 ]，忽略")
            
            i += 1
        
        # 检查未闭合的字符串
        if in_string:
            logger.warning(f"⚠️ 字符串未闭合，JSON可能不完整")
        
        # 提取结果
        if end > 0:
            result = text[:end]
            logger.debug(f"✅ JSON清洗完成，结果长度: {len(result)}")
        else:
            result = text
            logger.warning(f"⚠️ 未找到JSON结束位置，返回全部内容（长度: {len(result)}）")
            logger.debug(f"   栈状态: {stack}")
        
        # 验证清洗后的结果
        try:
            json.loads(result)
            logger.debug(f"✅ 清洗后JSON验证成功")
        except json.JSONDecodeError as e:
            logger.error(f"❌ 清洗后JSON仍然无效: {e}")
            logger.debug(f"   结果预览: {result[:500]}")
            logger.debug(f"   结果结尾: ...{result[-200:]}")
        
        return result
        
    except Exception as e:
        logger.error(f"❌ clean_json_response 出错: {e}")
        logger.error(f"   文本长度: {len(text) if text else 0}")
        logger.error(f"   文本预览: {text[:200] if text else 'None'}")
        raise


def parse_json(text: str) -> Union[Dict, List]:
    """解析 JSON，优先使用标准json，失败后用json5容错解析"""
    cleaned = clean_json_response(text)
    
    # 优先使用标准 json
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, Exception):
        pass
    
    # json5 容错解析（处理单引号、多余逗号、宽松格式等）
    if HAS_JSON5:
        try:
            logger.info("🔄 标准JSON解析失败，使用json5容错解析")
            result = json5.loads(cleaned)
            logger.info("✅ json5容错解析成功")
            return result
        except Exception as e5:
            logger.error(f"❌ json5容错解析也失败: {e5}")
    
    # 最终失败
    logger.error(f"❌ parse_json 完全失败")
    logger.error(f"   原始文本长度: {len(text) if text else 0}")
    logger.error(f"   清洗后文本长度: {len(cleaned) if cleaned else 0}")
    logger.debug(f"   清洗后文本预览: {cleaned[:500] if cleaned else 'None'}")
    raise json.JSONDecodeError("JSON解析失败（标准和json5均失败）", cleaned, 0)


def loads_json(text: str) -> Any:
    """
    json.loads 的容错替代品，可直接替换 json.loads()。
    优先用标准 json.loads，失败后自动降级到 json5。
    适用于解析 AI 返回的、可能包含不规范格式的 JSON。
    """
    # 优先使用标准 json
    try:
        return json.loads(text)
    except (json.JSONDecodeError, Exception):
        pass
    
    # json5 容错解析
    if HAS_JSON5:
        try:
            logger.info("🔄 json.loads失败，使用json5容错解析")
            result = json5.loads(text)
            logger.info("✅ json5容错解析成功")
            return result
        except Exception as e5:
            logger.error(f"❌ json5容错解析也失败: {e5}")
    
    # 最终失败，抛出标准异常
    raise json.JSONDecodeError("JSON解析失败（标准和json5均失败）", text, 0)
