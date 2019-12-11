package main

func isAttachmentMessage(data map[string]interface{}) []string {
	urls := make([]string, 0)
	blocks := data["blocks"]
	if blocks == nil {
		return urls
	}
	for _, blk := range blocks.([]map[string]interface{}) {
		elements := blk["elements"]
		if elements == nil {
			continue
		}
		for _, el := range elements.([]map[string]interface{}) {
			subEls := el["elements"]
			if subEls == nil {
				continue
			}
			for _, subel := range subEls.([]map[string]interface{}) {
				if subel["type"] == "link" {
					urls = append(urls, subel["url"].(string))
				}

			}
		}
	}
	return urls
}
